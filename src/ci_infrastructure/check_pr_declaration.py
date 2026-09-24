# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Verify that a pull request description ends with the ECMWF Contributor Declaration, verbatim.

The checker behind ``actions/check-pr-declaration``.

STDLIB ONLY, OLDER PYTHON, ON PURPOSE: the action runs it with the runner's bare
``python3`` (``tests/test_check_pr_declaration.py`` enforces the imports).

The verdict is tail equality of the normalized line lists (plus HIDDEN_BLOCK);
everything else only explains a failure.

SECURITY. The PR body is attacker-controlled: it is never printed raw, and
``$GITHUB_OUTPUT`` only receives a fixed ``Verdict`` value.
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

#: Matched exactly, as a whole line.
DECLARATION_HEADING: Final = "### Contributor Declaration"

#: Vendored from ecmwf/.github/.github/PULL_REQUEST_TEMPLATE.md, stored LF without the
#: org copy's trailing-space line (``normalize`` makes them equal).
#: ``.github/workflows/pr-declaration-drift.yml`` fails if the two diverge.
CANONICAL_DECLARATION: Final = """\
### Contributor Declaration

By opening this pull request, I affirm the following:

* All authors agree to the [Contributor License Agreement](https://github.com/ecmwf/codex/blob/main/Legal/Contributor-License-Agreement.md).
* The code follows the project's coding standards.
* I have performed self-review and added comments where needed.
* I have added or updated tests to verify that my changes are effective and functional.
* I have run all existing tests and confirmed they pass.
"""

#: Bots whose machine-generated PR bodies cannot carry the declaration.
#: Only honoured with ``user.type == "Bot"``.
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

SUMMARY_EXCERPT_LIMIT: Final = 2000

_LOOSE_HEADING: Final = re.compile(r"#{1,6}\s*contributor\s+declaration\s*$", re.IGNORECASE)
_DETAILS_OPEN: Final = re.compile(r"<details\b", re.IGNORECASE)
_DETAILS_CLOSE: Final = re.compile(r"</details\s*>", re.IGNORECASE)
_SUMMARY_OPEN: Final = re.compile(r"<summary\b", re.IGNORECASE)
_SUMMARY_CLOSE: Final = re.compile(r"</summary\s*>", re.IGNORECASE)
_BACKTICK_RUN: Final = re.compile(r"`+")

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


def normalize(text: str) -> list[str]:
    """Reduce ``text`` to the canonical line list the comparison runs on.

    1. Strip a leading U+FEFF.
    2. CRLF, then lone CR, to LF (the other order splits every CRLF twice).
    3. Split on ``"\\n"`` only; :meth:`str.splitlines` splits on characters GitHub renders inline.
    4. ``rstrip`` each line; never ``lstrip``, since leading whitespace is visible and semantic.
    5. Drop trailing blank lines.

    No interior blank collapsing and no Unicode folding: ``project’s`` must fail.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def expected_lines(source: str | None = None) -> list[str]:
    """Normalized declaration lines, from ``source`` (from its last heading on, so a whole PR template works) or the vendored constant."""
    lines = normalize(CANONICAL_DECLARATION if source is None else source)
    start = find_block_start(lines)
    if start is not None:
        lines = lines[start:]
    return lines


def body_from_event(payload: Mapping[str, Any]) -> str:
    """The PR body from a ``pull_request``/``pull_request_target`` payload; null becomes ``""``."""
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError(
            "event payload has no 'pull_request' object -- this action only runs on "
            "pull_request or pull_request_target events"
        )
    body = pull_request.get("body")
    return body if isinstance(body, str) else ""


def author_from_event(payload: Mapping[str, Any]) -> tuple[str, str]:
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
    """True for an allow-listed login whose GitHub-set ``user.type`` is Bot."""
    return kind == "Bot" and login in exempt


def parse_exempt_authors(raw: str) -> tuple[str, ...]:
    """Empty input means the default list."""
    entries = tuple(part.strip() for part in raw.split(",") if part.strip())
    return entries if entries else DEFAULT_EXEMPT_AUTHORS


def find_block_start(lines: Sequence[str], loose: bool = False) -> int | None:
    """Index of the last heading line, or None; last, so an earlier quoted example never anchors.

    ``loose`` (diagnosis only) also accepts any heading level or case.
    """
    for index in range(len(lines) - 1, -1, -1):
        if lines[index] == DECLARATION_HEADING or (loose and _LOOSE_HEADING.match(lines[index])):
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
    """Name the column and both codepoints of the first difference (e.g. a curly apostrophe)."""
    column = _first_diff_column(expected, actual)
    index = column - 1
    expected_char = expected[index] if index < len(expected) else None
    actual_char = actual[index] if index < len(actual) else None
    return f"column {column}: expected {_char_label(expected_char)}, found {_char_label(actual_char)}"


def first_divergence(actual: Sequence[str], expected: Sequence[str], offset: int) -> LineDiff | None:
    """First line of ``expected`` that ``actual`` (starting at body index ``offset``) fails to reproduce."""
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
    """Why the block, though present, is not visible: more openers than closers above it."""
    prefix = "\n".join(lines[:block_start])
    if prefix.count("<!--") > prefix.count("-->"):
        return "an unclosed HTML comment (<!--) above it, which hides the whole description"
    if len(_DETAILS_OPEN.findall(prefix)) > len(_DETAILS_CLOSE.findall(prefix)):
        return "an unclosed <details> element above it, which collapses it out of view"
    if len(_SUMMARY_OPEN.findall(prefix)) > len(_SUMMARY_CLOSE.findall(prefix)):
        return "an unclosed <summary> element above it, which hides it"
    return None


def check_body(body: str, expected: Sequence[str] | None = None) -> Result:
    """Judge ``body``; only the tail-equality comparison decides, the rest explains."""
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
        found = find_block_start(lines, loose=True)
    if found is None:
        return Result(
            Verdict.HEADING_MISSING,
            f"the Contributor Declaration is gone entirely -- no '{DECLARATION_HEADING}' heading in the description.",
        )

    tail = lines[found:]
    if tail[: len(declaration)] == declaration:
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


def _strip_control(text: str) -> str:
    """Drop C0/C1 control characters (ANSI sequences) and lone surrogates (would raise on write)."""
    stripped = "".join(char for char in text if unicodedata.category(char) != "Cc")
    return stripped.encode("utf-8", "replace").decode("utf-8", "replace")


def _escape_command(text: str) -> str:
    """Escape a workflow-command payload onto one line, so an embedded ``::stop-commands::`` never starts a line."""
    return _strip_control(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_error(result: Result) -> str:
    hint = _HINTS.get(result.verdict.value, "")
    return f"::error title=Contributor Declaration::{_escape_command(result.headline)} {_escape_command(hint)} {_escape_command(_EDIT_HINT)}"


def _escape_markdown(text: str) -> str:
    """Escape HTML (summaries render ``<img>``/``<a>``) in the headline; other untrusted text is fenced."""
    return _strip_control(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fence(lines: Sequence[str]) -> str:
    """Fence untrusted lines, truncated, with a backtick run longer than any inside them."""
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


def _read_text(path: Path) -> str:
    """Read ``path`` verbatim: no newline translation, invalid UTF-8 kept as surrogates."""
    with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return handle.read()


def _append(env_var: str, text: str) -> None:
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
        "--declaration-file",
        help="File holding the declaration to require. Everything from the last heading occurrence is used, "
        "so a whole PR template can be passed. Defaults to the vendored canonical text.",
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

    declaration_source = _read_text(Path(args.declaration_file)) if args.declaration_file else None
    declaration = expected_lines(declaration_source)
    result = check_body(body, declaration)

    # Annotation first, so later output cannot suppress it.
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
