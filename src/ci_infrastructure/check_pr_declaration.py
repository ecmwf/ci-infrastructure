# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Verify that a pull request description ends with the ECMWF Contributor Declaration.

ECMWF's public repos ship a PR template whose last section is the Contributor
Declaration -- the CLA affirmation plus the contributor checklist. It is plain
template text, so nothing stops a contributor from deleting or rewording it while
filling in the description, which silently removes the affirmation the project
relies on. This module is the checker behind
``actions/check-pr-declaration``: it fails when the description does not END with
that block, verbatim.

STDLIB ONLY, ON PURPOSE. The action runs this with whatever ``python3`` the
consumer's runner happens to have, with no venv and no ``pip install`` -- see the
action's description for why ``ensure-infrastructure-present`` is deliberately not
used. Do not import ``click``, ``pydantic``, or even this package's own
``._errors`` (which imports click). ``tests/test_check_pr_declaration.py`` asserts
this with an AST walk, because in CI the package IS installed and a stray import
would only explode on a consumer's runner.

The pass/fail decision is one expression -- tail equality of the normalized line
lists -- so no heuristic can make a bad body pass. Everything else in here
(heading hunting, loose heading matching, character-level diffing) exists solely
to produce a diagnosis a contributor can act on, and runs only after that
expression has already said no.

The one exception is HIDDEN_BLOCK: a body that is an unclosed ``<!--`` followed by
the verbatim block satisfies tail equality while GitHub renders the whole
description as an HTML comment, so the declaration is invisible to every human
reader. That is checked on the passing path too.

SECURITY. The PR body is attacker-controlled text (up to 65 536 characters, from
anonymous fork authors) that we echo into the runner log, an annotation and the
step summary. Therefore: the raw body is never printed; at most one line of it
appears, via ``repr()``, inside a single-line annotation whose newlines are
escaped, so an embedded ``::stop-commands::`` is never at the start of a line and
stays inert; C0/C1 control characters are stripped so ANSI sequences cannot hide
or fake log output; summary excerpts are fenced with a backtick run longer than
any in the content and hard-truncated; and ``$GITHUB_OUTPUT`` only ever receives a
fixed ``Verdict`` value, never body-derived text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Final

#: The heading that opens the declaration. Matched exactly, as a whole line.
DECLARATION_HEADING: Final = "### Contributor Declaration"

#: Vendored from ecmwf/.github/.github/PULL_REQUEST_TEMPLATE.md (verified 2026-08-17).
#:
#: Stored LF-terminated with no trailing-whitespace line, unlike the org copy
#: (CRLF, ending ``pass.\r\n \r\n``); ``normalize`` makes the two equivalent, and
#: this form survives the repo's ``trailing-whitespace`` / ``end-of-file-fixer``
#: pre-commit hooks unchanged. ``.github/workflows/pr-declaration-drift.yml``
#: fails if the org template diverges from this constant.
CANONICAL_DECLARATION: Final = """\
### Contributor Declaration

By opening this pull request, I affirm the following:

* All authors agree to the [Contributor License Agreement](https://github.com/ecmwf/codex/blob/main/Legal/Contributor-License-Agreement.md).
* The code follows the project's coding standards.
* I have performed self-review and added comments where needed.
* I have added or updated tests to verify that my changes are effective and functional.
* I have run all existing tests and confirmed they pass.
"""

#: Bot accounts whose PR bodies are machine-generated and cannot carry the
#: declaration. Dependabot in particular offers no footer customisation and
#: rewrites the body on every rebase, so a manual fix does not survive; without
#: an exemption a required check would permanently block every such PR. Only
#: honoured when the payload also reports ``user.type == "Bot"``, so a human
#: account named ``dependabot`` is not exempt.
DEFAULT_EXEMPT_AUTHORS: Final = (
    "dependabot[bot]",
    "renovate[bot]",
    "pre-commit-ci[bot]",
    "release-please[bot]",
    "github-actions[bot]",
    "copilot-swe-agent[bot]",
)

#: Fail closed above this many characters. GitHub caps PR bodies at 65 536, so a
#: body anywhere near this is not a real description.
MAX_BODY_CHARS: Final = 200_000

#: Per-excerpt cap for the step summary.
SUMMARY_EXCERPT_LIMIT: Final = 2000

_LOOSE_HEADING: Final = re.compile(r"#{1,6}\s*contributor\s+declaration\s*$", re.IGNORECASE)
_DETAILS_OPEN: Final = re.compile(r"<details\b", re.IGNORECASE)
_DETAILS_CLOSE: Final = re.compile(r"</details\s*>", re.IGNORECASE)
_SUMMARY_OPEN: Final = re.compile(r"<summary\b", re.IGNORECASE)
_SUMMARY_CLOSE: Final = re.compile(r"</summary\s*>", re.IGNORECASE)
_BACKTICK_RUN: Final = re.compile(r"`+")

#: Per-verdict closing sentence for the annotation. Kept separate from the
#: headline so each failure mode gets the fix that actually applies to it -- a
#: generic "restore the block" is wrong advice for NOT_AT_END, where the block is
#: already intact.
_HINTS: Final = {
    "empty-body": "Paste the declaration from the job summary at the end of the description.",
    "heading-missing": "Copy the block from the job summary and paste it at the end of the description.",
    "diverged": "Replace your version with the block from the job summary, character for character.",
    "not-at-end": "Cut the trailing lines and paste them above the declaration.",
    "hidden-block": "Close the element that swallows it, or move the declaration above it.",
}

_EDIT_HINT: Final = "Editing the description re-runs this check; no push is needed."


class Verdict(str, Enum):
    """Outcome of a check. The value is what lands in the action's ``verdict`` output."""

    OK = "ok"
    BOT_EXEMPT = "bot-exempt"
    EMPTY_BODY = "empty-body"
    HEADING_MISSING = "heading-missing"
    NOT_AT_END = "not-at-end"
    HIDDEN_BLOCK = "hidden-block"
    DIVERGED = "diverged"


@dataclass(frozen=True)
class LineDiff:
    """Where and how the body first departs from the declaration."""

    index: int
    """0-based position within the declaration block."""

    body_line_no: int
    """1-based line number in the normalized body."""

    expected: str
    actual: str
    """The body's line, or ``""`` when the body ran out of lines."""

    missing: bool
    """True when the body ended before this line of the declaration."""

    column: int
    """1-based first differing column, or 0 when ``missing``."""


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    headline: str
    """One sentence, safe to put in an annotation. Never contains raw body text."""

    diff: LineDiff | None = None
    trailing: tuple[str, ...] = field(default_factory=tuple)
    """Lines found after the block (NOT_AT_END only)."""

    block_start: int | None = None
    """1-based line number of the declaration heading in the body, when located."""

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.OK, Verdict.BOT_EXEMPT)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #


def normalize(text: str) -> list[str]:
    """Reduce ``text`` to the canonical line list the comparison runs on.

    The steps, in this order, are each forced by real data:

    1. Strip a leading U+FEFF -- a body pasted from a Windows editor would
       otherwise fail invisibly on line 1.
    2. CRLF and lone CR to LF. The org template is CRLF, GitHub's web form
       submits CRLF, and real PR bodies are a mix (of four anemoi-core PRs
       sampled, three were LF-only and one CRLF). CRLF must be replaced *first*:
       handling lone CR first would split every CRLF into two lines.
    3. Split on ``"\\n"`` and never with :meth:`str.splitlines`, which also
       splits on U+0085, U+000B, U+000C, U+001C-1E, U+2028 and U+2029 -- that
       would decouple our line model from GitHub's rendering and let a body
       containing those characters normalize to something the reader never sees.
    4. ``rstrip`` each line, trailing only. This absorbs the org template's stray
       space-only final line and any editor-inserted trailing spaces, which are
       invisible in GitHub's editor and would otherwise produce a red check
       nobody can fix by looking. Markdown's two-space hard break renders
       identically to no break, so dropping it is semantically right too. There
       is deliberately no ``lstrip``: leading whitespace IS visible and IS
       semantic (four spaces make a code block, indenting a ``*`` changes list
       nesting), so an indented bullet must fail.
    5. Drop trailing blank lines -- required by the real data, since after step 4
       the org template's tail is ``["...they pass.", ""]``, and contributors
       leave arbitrary trailing newlines. "Nothing may follow the block" means
       nothing *meaningful*.

    Deliberately NOT done: collapsing interior blank runs (the block's internal
    blank lines are part of the canonical text and the reported line numbers
    depend on them), and any Unicode or quote folding -- this is a legal
    declaration, so ``project's`` becoming ``project’s`` has to fail. That is
    survivable only because :func:`describe_char_diff` names the column and both
    codepoints.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def expected_lines() -> list[str]:
    """The declaration as normalized lines.

    There is deliberately no way to substitute a different text: one declaration
    org-wide is the whole point, and a repo whose template differs is a repo
    whose template needs fixing. ``check_body`` still takes an ``expected``
    argument, but only so tests can exercise the comparison directly.
    """
    return normalize(CANONICAL_DECLARATION)


# --------------------------------------------------------------------------- #
# Event payload
# --------------------------------------------------------------------------- #


def body_from_event(payload: Mapping[str, Any]) -> str:
    """Extract the PR body from a ``pull_request``/``pull_request_target`` payload.

    A JSON ``null`` body becomes ``""``, which normalizes to no lines and is
    reported as EMPTY_BODY rather than as a mismatch.
    """
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError(
            "event payload has no 'pull_request' object -- this action only runs on "
            "pull_request or pull_request_target events"
        )
    body = pull_request.get("body")
    return body if isinstance(body, str) else ""


def author_from_event(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(login, type)`` for the PR author, ``("", "")`` when absent."""
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return ("", "")
    user = pull_request.get("user")
    if not isinstance(user, dict):
        return ("", "")
    login = user.get("login")
    kind = user.get("type")
    return (login if isinstance(login, str) else "", kind if isinstance(kind, str) else "")


def is_exempt(login: str, kind: str, exempt: Sequence[str]) -> bool:
    """True when this author is an allow-listed bot.

    Both conditions are required. ``user.type`` is set by GitHub and cannot be
    spoofed by a contributor, so a human account named ``dependabot`` stays
    subject to the check.
    """
    return kind == "Bot" and login in exempt


def parse_exempt_authors(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated allowlist; empty input means the default list."""
    entries = tuple(part.strip() for part in raw.split(",") if part.strip())
    return entries if entries else DEFAULT_EXEMPT_AUTHORS


# --------------------------------------------------------------------------- #
# Locating and diffing the block
# --------------------------------------------------------------------------- #


def find_block_start(lines: Sequence[str], heading: str = DECLARATION_HEADING) -> int | None:
    """Index of the *last* line equal to ``heading``, or None.

    Last, not first: the block belongs at the end, so a quoted example earlier in
    the description must not anchor the diagnosis.
    """
    for index in range(len(lines) - 1, -1, -1):
        if lines[index] == heading:
            return index
    return None


def find_loose_block_start(lines: Sequence[str]) -> int | None:
    """Index of the last line that looks like the heading at any level or case.

    Diagnosis only. Lets ``## Contributor declaration`` be reported as "line 12:
    expected ..., got ..." instead of the blunt "the section is gone entirely".
    """
    for index in range(len(lines) - 1, -1, -1):
        if _LOOSE_HEADING.match(lines[index]):
            return index
    return None


def _first_diff_column(expected: str, actual: str) -> int:
    limit = min(len(expected), len(actual))
    for index in range(limit):
        if expected[index] != actual[index]:
            return index + 1
    return limit + 1


def _char_label(char: str | None) -> str:
    if char is None:
        return "end of line"
    name = unicodedata.name(char, "")
    codepoint = f"U+{ord(char):04X}"
    if name:
        return f"{char!r} ({codepoint} {name})"
    return f"{char!r} ({codepoint})"


def describe_char_diff(expected: str, actual: str) -> str:
    """Name the column and both codepoints of the first difference.

    This is what makes an autocorrected apostrophe or a stray NBSP diagnosable
    rather than maddening, given that :func:`normalize` deliberately does no
    Unicode folding.
    """
    column = _first_diff_column(expected, actual)
    index = column - 1
    expected_char = expected[index] if index < len(expected) else None
    actual_char = actual[index] if index < len(actual) else None
    return f"column {column}: expected {_char_label(expected_char)}, found {_char_label(actual_char)}"


def first_divergence(actual: Sequence[str], expected: Sequence[str], offset: int) -> LineDiff | None:
    """First line of ``expected`` that ``actual`` fails to reproduce.

    ``offset`` is the 0-based index in the body at which ``actual`` starts, so
    the returned ``body_line_no`` is a line number the contributor can count to.
    """
    for index, expected_line in enumerate(expected):
        if index >= len(actual):
            return LineDiff(
                index=index,
                body_line_no=offset + index + 1,
                expected=expected_line,
                actual="",
                missing=True,
                column=0,
            )
        if actual[index] != expected_line:
            return LineDiff(
                index=index,
                body_line_no=offset + index + 1,
                expected=expected_line,
                actual=actual[index],
                missing=False,
                column=_first_diff_column(expected_line, actual[index]),
            )
    return None


def _hidden_reason(lines: Sequence[str], block_start: int) -> str | None:
    """Why the block, though present, would not be visible to a reader.

    Counts openers against closers rather than merely spotting an opener, so a
    body that legitimately closes a ``<details>`` log dump above the declaration
    is not penalised.
    """
    prefix = "\n".join(lines[:block_start])
    if prefix.count("<!--") > prefix.count("-->"):
        return "an unclosed HTML comment (<!--) above it, which hides the whole description"
    if len(_DETAILS_OPEN.findall(prefix)) > len(_DETAILS_CLOSE.findall(prefix)):
        return "an unclosed <details> element above it, which collapses it out of view"
    if len(_SUMMARY_OPEN.findall(prefix)) > len(_SUMMARY_CLOSE.findall(prefix)):
        return "an unclosed <summary> element above it, which hides it"
    return None


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #


def check_body(body: str, expected: Sequence[str] | None = None) -> Result:
    """Judge ``body`` against the declaration.

    The decision is the single tail-equality comparison below. Everything after
    it only explains a failure, so no diagnostic heuristic can turn a bad body
    into a pass.
    """
    declaration = list(expected) if expected is not None else expected_lines()
    lines = normalize(body)

    if not lines:
        return Result(
            Verdict.EMPTY_BODY,
            "the pull request description is empty; it must end with the Contributor Declaration.",
        )

    if len(lines) >= len(declaration) and lines[len(lines) - len(declaration) :] == declaration:
        tail_start = len(lines) - len(declaration)
        reason = _hidden_reason(lines, tail_start)
        if reason is None:
            return Result(
                Verdict.OK,
                "the description ends with the Contributor Declaration.",
                block_start=tail_start + 1,
            )
        return Result(
            Verdict.HIDDEN_BLOCK,
            f"the Contributor Declaration is present but there is {reason}.",
            block_start=tail_start + 1,
        )

    found = find_block_start(lines)
    if found is None:
        found = find_loose_block_start(lines)
    if found is None:
        return Result(
            Verdict.HEADING_MISSING,
            f"the Contributor Declaration is gone entirely -- no '{DECLARATION_HEADING}' heading in the description.",
        )

    tail = lines[found:]
    if tail[: len(declaration)] == declaration:
        # Blank lines after the block carry no meaning -- only the lines a reader
        # would actually see are worth counting or showing.
        trailing = tuple(line for line in tail[len(declaration) :] if line)
        return Result(
            Verdict.NOT_AT_END,
            f"the Contributor Declaration is intact but {len(trailing)} line(s) follow it; it has to be the last thing "
            "in the description, so extra notes (a 'Fixes #123', a screenshot, a release note) go above it.",
            trailing=trailing,
            block_start=found + 1,
        )

    diff = first_divergence(tail, declaration, found)
    if diff is None:  # pragma: no cover - tail equality already ruled this out
        return Result(Verdict.DIVERGED, "the Contributor Declaration does not match.", block_start=found + 1)

    if diff.missing:
        headline = f"the Contributor Declaration is truncated: line {diff.body_line_no} of the description should be {diff.expected!r}."
    else:
        headline = f"line {diff.body_line_no} of the description should be {diff.expected!r} but is {diff.actual!r} ({describe_char_diff(diff.expected, diff.actual)})."
    return Result(Verdict.DIVERGED, headline, diff=diff, block_start=found + 1)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _strip_control(text: str) -> str:
    """Drop C0/C1 control characters so ANSI sequences cannot reach the log.

    Without this, a body could clear the log view (``\\x1b[2J``), hide text
    (``\\x1b[8m``), or paint a fake green "passed" next to the real failure.

    Surrogates are folded out in the same pass. A body read with
    ``errors="surrogateescape"`` can carry lone surrogates, and those raise
    ``UnicodeEncodeError`` the moment we write them to the step summary -- turning
    a clean verdict into a traceback.
    """
    stripped = "".join(char for char in text if unicodedata.category(char) != "Cc")
    return stripped.encode("utf-8", "replace").decode("utf-8", "replace")


def _escape_command(text: str) -> str:
    """Escape a workflow-command payload so it stays on one line.

    Newlines are what matter: a workflow command is only recognised at the start
    of a line, so with them escaped an embedded ``::stop-commands::`` from the
    body can never take effect.
    """
    return _strip_control(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_error(result: Result) -> str:
    """The single-line ``::error::`` annotation for a failing result."""
    hint = _HINTS.get(result.verdict.value, "")
    return f"::error title=Contributor Declaration::{_escape_command(result.headline)} {_escape_command(hint)} {_escape_command(_EDIT_HINT)}"


def _escape_markdown(text: str) -> str:
    """Neutralise HTML in text that lands in the summary outside a code fence.

    Step summaries render a sanitized HTML subset that still allows ``<img>``,
    ``<a>`` and ``<details>``, so an unescaped body excerpt is an IP beacon and a
    phishing surface aimed at whoever opens the run. Only the headline needs this;
    everything else untrusted goes through :func:`_fence`.
    """
    return _strip_control(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fence(lines: Sequence[str]) -> str:
    """Fence untrusted lines in a code block they cannot escape.

    The fence is one backtick longer than the longest run in the content, and
    control characters are stripped. Content inside a fenced block is not
    HTML-interpreted, so the ``<img>`` beacons and ``<a>`` phishing links a body
    could aim at the maintainer reading the summary stay inert as text.
    """
    body = "\n".join(_strip_control(line) for line in lines)
    if len(body) > SUMMARY_EXCERPT_LIMIT:
        body = body[:SUMMARY_EXCERPT_LIMIT] + "\n... (truncated)"
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body}\n{fence}"


def render_summary(result: Result, expected: Sequence[str]) -> str:
    """Markdown for ``$GITHUB_STEP_SUMMARY``: what is wrong, and what to paste."""
    hint = _HINTS.get(result.verdict.value, "")
    parts = ["## Contributor Declaration check failed", "", _escape_markdown(result.headline), ""]
    if hint:
        parts += [hint, ""]

    if result.diff is not None and not result.diff.missing:
        parts += [
            f"Line {result.diff.body_line_no} of your description reads:",
            "",
            _fence([result.diff.actual]),
            "",
            "and it has to read:",
            "",
            _fence([result.diff.expected]),
            "",
        ]
    elif result.diff is not None:
        parts += [
            f"Your description stops at line {result.diff.body_line_no - 1}, before the declaration is complete.",
            "",
        ]
    elif result.trailing:
        parts += [
            f"These {len(result.trailing)} line(s) come after the declaration and have to move above it:",
            "",
            _fence(result.trailing),
            "",
        ]

    parts += [
        "Your description must **end** with exactly this block:",
        "",
        "```text",
        "\n".join(expected),
        "```",
        "",
        _EDIT_HINT,
        "",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _read_text(path: Path) -> str:
    """Read ``path`` verbatim.

    ``newline=""`` matters: universal-newline translation would otherwise do step
    2 of :func:`normalize` behind our back, making the CRLF tests vacuous on this
    path while the event-payload path (where ``\\r\\n`` survives inside a JSON
    string) behaved differently. ``surrogateescape`` turns invalid UTF-8 into a
    verdict instead of a traceback.
    """
    with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return handle.read()


def _append(env_var: str, text: str) -> None:
    """Append to a GitHub file-command target, if we are running under Actions."""
    path = os.environ.get(env_var)
    if not path:
        return
    with open(path, "a", encoding="utf-8", errors="replace") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci-infrastructure-check-declaration",
        description="Fail unless a pull request description ends with the ECMWF Contributor Declaration.",
    )
    parser.add_argument(
        "--body-file",
        help="File holding the PR description verbatim. Takes precedence over --event-file for the body.",
    )
    parser.add_argument(
        "--event-file",
        help="GitHub event payload ($GITHUB_EVENT_PATH). Source of the body when --body-file is absent, "
        "and the only source of author information for the bot allowlist.",
    )
    parser.add_argument(
        "--exempt-authors",
        default="",
        help="Comma-separated bot logins to skip (only when the payload reports user.type == 'Bot'). "
        "Empty means the built-in list.",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Do not write to $GITHUB_STEP_SUMMARY.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Return 0 when the description is compliant or the author is an exempt bot."""
    args = _build_parser().parse_args(argv)

    if not args.body_file and not args.event_file:
        print("::error title=Contributor Declaration::one of --body-file or --event-file is required", file=sys.stderr)
        return 2

    payload: Mapping[str, Any] = {}
    if args.event_file:
        try:
            payload = json.loads(_read_text(Path(args.event_file)))
        except (OSError, ValueError) as exc:
            print(
                f"::error title=Contributor Declaration::cannot read the event payload: {_escape_command(str(exc))}",
                file=sys.stderr,
            )
            return 2

    if payload:
        login, kind = author_from_event(payload)
        if is_exempt(login, kind, parse_exempt_authors(args.exempt_authors)):
            print(
                f"::notice title=Contributor Declaration::skipped: {_escape_command(login)} is an allow-listed bot account, whose PR "
                "description is machine-generated and cannot carry the declaration."
            )
            _append("GITHUB_OUTPUT", f"verdict={Verdict.BOT_EXEMPT.value}")
            return 0

    try:
        if args.body_file:
            body = _read_text(Path(args.body_file))
        else:
            body = body_from_event(payload)
    except (OSError, ValueError) as exc:
        print(
            f"::error title=Contributor Declaration::cannot read the pull request description: {_escape_command(str(exc))}",
            file=sys.stderr,
        )
        return 2

    if len(body) > MAX_BODY_CHARS:
        print(
            f"::error title=Contributor Declaration::the description is {len(body)} characters, above the {MAX_BODY_CHARS} character "
            "limit this check accepts; that is not a real description.",
            file=sys.stderr,
        )
        _append("GITHUB_OUTPUT", f"verdict={Verdict.DIVERGED.value}")
        return 1

    declaration = expected_lines()
    result = check_body(body, declaration)

    # The annotation goes out before anything else, so it cannot be suppressed by
    # output emitted later in the job.
    if result.ok:
        print(f"Contributor Declaration: {result.headline}")
    else:
        print(render_error(result))
        if not args.no_summary:
            _append("GITHUB_STEP_SUMMARY", render_summary(result, declaration))

    _append("GITHUB_OUTPUT", f"verdict={result.verdict.value}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
