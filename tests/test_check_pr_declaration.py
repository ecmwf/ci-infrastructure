# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Contributor Declaration checker.

Whitespace fixtures are built in Python: the pre-commit whitespace hooks would "fix" them on disk.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from ci_infrastructure.check_pr_declaration import (
    CANONICAL_DECLARATION,
    DECLARATION_HEADING,
    DEFAULT_EXEMPT_AUTHORS,
    MAX_BODY_CHARS,
    Verdict,
    author_from_event,
    body_from_event,
    check_body,
    describe_char_diff,
    expected_lines,
    is_exempt,
    main,
    normalize,
    parse_exempt_authors,
    render_error,
    render_summary,
)

ROOT: Final = Path(__file__).resolve().parents[1]
MODULE_PATH: Final = ROOT / "src" / "ci_infrastructure" / "check_pr_declaration.py"

#: ecmwf/.github/.github/PULL_REQUEST_TEMPLATE.md, byte for byte as the API serves
#: it: CRLF throughout, two blank lines under "### Description", and a final
#: space-only line. 518 bytes, asserted below.
ORG_TEMPLATE: Final = (
    "### Description\r\n"
    "\r\n"
    "\r\n"
    "### Contributor Declaration\r\n"
    "\r\n"
    "By opening this pull request, I affirm the following:\r\n"
    "\r\n"
    "* All authors agree to the [Contributor License Agreement]"
    "(https://github.com/ecmwf/codex/blob/main/Legal/Contributor-License-Agreement.md).\r\n"
    "* The code follows the project's coding standards.\r\n"
    "* I have performed self-review and added comments where needed.\r\n"
    "* I have added or updated tests to verify that my changes are effective and functional.\r\n"
    "* I have run all existing tests and confirmed they pass.\r\n"
    " \r\n"
)

#: The one-line variant ecmwf/anemoi-core and ecmwf/ellem ship today. It must
#: fail until those repos converge on the org block.
ANEMOI_TAIL: Final = (
    "By opening this pull request, I affirm that all authors agree to the "
    "[Contributor License Agreement.]"
    "(https://github.com/ecmwf/codex/blob/main/Legal/Contributor-License-Agreement.md)\n"
)

DECLARATION_LINES: Final = normalize(CANONICAL_DECLARATION)


def body(*, prefix: str = "## Description\n\nsomething\n\n", declaration: str | None = None) -> str:
    """A plausible PR body: some prose, then the declaration."""
    return prefix + (CANONICAL_DECLARATION if declaration is None else declaration)


def event(body_text: str | None, login: str = "someone", kind: str = "User") -> dict[str, Any]:
    return {"pull_request": {"number": 7, "body": body_text, "user": {"login": login, "type": kind}}}


Runner = Callable[..., tuple[int, str, str, str]]


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Runner:
    """Run ``main`` with the Actions file commands and any `body`/`payload` files under ``tmp_path``."""

    def _run(
        *argv: str, body: str | bytes | None = None, payload: dict[str, Any] | None = None
    ) -> tuple[int, str, str, str]:
        args = list(argv)
        if body is not None:
            body_file = tmp_path / "body.md"
            if isinstance(body, bytes):
                body_file.write_bytes(body)
            else:
                body_file.write_text(body, encoding="utf-8", newline="")
            args += ["--body-file", str(body_file)]
        if payload is not None:
            event_file = tmp_path / "event.json"
            event_file.write_text(json.dumps(payload), encoding="utf-8")
            args += ["--event-file", str(event_file)]
        summary = tmp_path / "summary.md"
        output = tmp_path / "output.txt"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        code = main(args)
        captured = capsys.readouterr()
        return (
            code,
            captured.out + captured.err,
            summary.read_text(encoding="utf-8") if summary.exists() else "",
            output.read_text(encoding="utf-8") if output.exists() else "",
        )

    return _run


# === Stdlib purity ===

STDLIB_ALLOWLIST: Final = frozenset(
    {
        "__future__",
        "argparse",
        "collections",
        "dataclasses",
        "enum",
        "json",
        "os",
        "pathlib",
        "re",
        "sys",
        "typing",
        "unicodedata",
    }
)


def test_module_imports_only_stdlib() -> None:
    """The action runs this with a bare python3; CI has the package installed, so only the AST check catches it."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"relative import {node.module!r} would drag in the package's dependencies"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= STDLIB_ALLOWLIST, f"non-stdlib imports: {sorted(imported - STDLIB_ALLOWLIST)}"


# === Real data ===


def test_org_template_passes_unmodified() -> None:
    """CRLF and a trailing space-only line, as GitHub serves it."""
    assert check_body(ORG_TEMPLATE).verdict is Verdict.OK


def test_rstrip_happens_before_trailing_blanks_are_dropped() -> None:
    assert normalize(ORG_TEMPLATE)[-1] == "* I have run all existing tests and confirmed they pass."


def test_anemoi_variant_fails() -> None:
    result = check_body("## Description\n\nwork\n\n" + ANEMOI_TAIL)
    assert result.verdict is Verdict.HEADING_MISSING


def test_canonical_block_shape() -> None:
    assert len(DECLARATION_LINES) == 9
    assert DECLARATION_LINES[0] == DECLARATION_HEADING
    assert DECLARATION_LINES[-1].endswith("they pass.")
    assert "project's" in DECLARATION_LINES[5], "the apostrophe must stay ASCII U+0027"


# === Normalization ===


def test_normalize_is_idempotent() -> None:
    once = normalize(ORG_TEMPLATE)
    assert normalize("\n".join(once)) == once


@pytest.mark.parametrize("eol", ["\n", "\r", "\r\n"], ids=["lf", "cr", "crlf"])
def test_any_line_ending_passes(eol: str) -> None:
    assert check_body(body().replace("\n", eol)).verdict is Verdict.OK


def test_per_line_trailing_whitespace_ignored() -> None:
    padded = "\n".join(line + "   \t" for line in body().split("\n"))
    assert check_body(padded).verdict is Verdict.OK


@pytest.mark.parametrize("count", [1, 5, 50])
def test_trailing_blank_lines_ignored(count: int) -> None:
    assert check_body(body() + "\n" * count).verdict is Verdict.OK


def test_trailing_whitespace_only_lines_ignored() -> None:
    assert check_body(body() + "   \n\t\n \n").verdict is Verdict.OK


def test_leading_bom_ignored() -> None:
    assert check_body(chr(0xFEFF) + body()).verdict is Verdict.OK


def test_indented_bullet_fails() -> None:
    """No ``lstrip``: leading whitespace is visible."""
    result = check_body(body().replace("* I have run all", "    * I have run all"))
    assert result.verdict is Verdict.DIVERGED


def test_unicode_line_separator_does_not_split_lines() -> None:
    """U+2028 is why :meth:`str.splitlines` is banned."""
    tampered = body().replace("they pass.", "they" + chr(0x2028) + "pass.")
    lines = normalize(tampered)
    assert len(lines) == len(normalize(body()))
    assert check_body(tampered).verdict is Verdict.DIVERGED


# === Verdicts ===


@pytest.mark.parametrize("text", ["", "   \n\t\n", "\n\n\n"])
def test_empty_body(text: str) -> None:
    result = check_body(text)
    assert result.verdict is Verdict.EMPTY_BODY
    assert "empty" in result.headline


def test_null_body_from_event_is_empty() -> None:
    assert body_from_event(event(None)) == ""
    assert check_body(body_from_event(event(None))).verdict is Verdict.EMPTY_BODY


def test_heading_absent() -> None:
    assert check_body("## Description\n\njust some prose\n").verdict is Verdict.HEADING_MISSING


def test_loose_heading_is_diagnosed_as_diverged() -> None:
    """``## Contributor declaration`` deserves a line number, not a blunt refusal."""
    result = check_body(body().replace(DECLARATION_HEADING, "## Contributor declaration"))
    assert result.verdict is Verdict.DIVERGED
    assert result.diff is not None
    assert result.diff.index == 0


def test_text_after_block() -> None:
    result = check_body(body() + "\nFixes #12\n")
    assert result.verdict is Verdict.NOT_AT_END
    assert result.trailing == ("Fixes #12",)


def test_html_comment_after_block_is_not_exempt() -> None:
    result = check_body(body() + "\n<!-- preview: https://example.invalid -->\n")
    assert result.verdict is Verdict.NOT_AT_END


def test_unclosed_comment_before_block_is_hidden() -> None:
    """The one hole in a pure ends-with rule: green check, invisible declaration."""
    result = check_body("<!--\n" + body())
    assert result.verdict is Verdict.HIDDEN_BLOCK


def test_balanced_comment_before_block_passes() -> None:
    assert check_body("<!-- a note -->\n" + body()).verdict is Verdict.OK


def test_unclosed_details_before_block_is_hidden() -> None:
    result = check_body("<details><summary>logs</summary>\n\n" + body())
    assert result.verdict is Verdict.HIDDEN_BLOCK


def test_closed_details_before_block_passes() -> None:
    prefix = "<details><summary>logs</summary>\n\nboring output\n\n</details>\n\n"
    assert check_body(prefix + CANONICAL_DECLARATION).verdict is Verdict.OK


def test_bullet_reworded_reports_line_and_content() -> None:
    tampered = body().replace(
        "* The code follows the project's coding standards.",
        "* The code follows the coding standards.",
    )
    result = check_body(tampered)
    assert result.verdict is Verdict.DIVERGED
    assert result.diff is not None
    assert result.diff.index == 5
    assert result.diff.expected == "* The code follows the project's coding standards."
    assert result.diff.actual == "* The code follows the coding standards."
    assert str(result.diff.body_line_no) in result.headline


def test_bullet_deleted_reports_first_divergence() -> None:
    tampered = body().replace("* I have performed self-review and added comments where needed.\n", "")
    result = check_body(tampered)
    assert result.verdict is Verdict.DIVERGED
    assert result.diff is not None
    assert result.diff.index == 6
    assert result.diff.expected == "* I have performed self-review and added comments where needed."


def test_truncated_block_reports_missing_line() -> None:
    truncated = body(declaration="\n".join(DECLARATION_LINES[:-2]) + "\n")
    result = check_body(truncated)
    assert result.verdict is Verdict.DIVERGED
    assert result.diff is not None
    assert result.diff.missing is True
    assert result.diff.actual == ""
    assert result.diff.column == 0


def test_smart_apostrophe_reports_codepoints() -> None:
    result = check_body(body().replace("project's", "project" + chr(0x2019) + "s"))
    assert result.verdict is Verdict.DIVERGED
    assert "U+0027" in result.headline
    assert "U+2019" in result.headline


def test_nbsp_reports_codepoints() -> None:
    result = check_body(body().replace("I affirm", "I" + chr(0xA0) + "affirm"))
    assert result.verdict is Verdict.DIVERGED
    assert "U+00A0" in result.headline


def test_describe_char_diff_names_end_of_line() -> None:
    assert "end of line" in describe_char_diff("abc", "ab")


def test_last_heading_wins() -> None:
    """A quoted example earlier in the body must not anchor the diagnosis."""
    tampered = body() + "\n" + "\n".join(DECLARATION_LINES[:-1]) + "\n* I have run all existing tests.\n"
    result = check_body(tampered)
    assert result.verdict is Verdict.DIVERGED
    assert result.diff is not None
    assert result.diff.index == 8


# === Declaration source override ===


def test_declaration_file_override() -> None:
    custom = "### Contributor Declaration\n\nI agree to everything.\n"
    assert check_body("prose\n\n" + custom, expected_lines(custom)).verdict is Verdict.OK
    assert check_body(body(), expected_lines(custom)).verdict is Verdict.DIVERGED


def test_declaration_source_is_sliced_from_the_heading() -> None:
    """A whole PR template can be passed; only the block is required."""
    assert expected_lines(ORG_TEMPLATE) == DECLARATION_LINES


def test_declaration_source_without_heading_is_used_whole() -> None:
    assert expected_lines("just this line\n") == ["just this line"]


# === Event payload and the bot allowlist ===


def test_body_from_event_requires_a_pull_request() -> None:
    with pytest.raises(ValueError, match="pull_request"):
        body_from_event({"repository": {}})


def test_author_from_event() -> None:
    assert author_from_event(event("x", "dependabot[bot]", "Bot")) == ("dependabot[bot]", "Bot")
    assert author_from_event({}) == ("", "")
    assert author_from_event({"pull_request": {}}) == ("", "")


def test_exemption_requires_the_bot_account_type() -> None:
    """A human account named ``dependabot`` is not exempt."""
    assert is_exempt("dependabot[bot]", "Bot", DEFAULT_EXEMPT_AUTHORS) is True
    assert is_exempt("dependabot[bot]", "User", DEFAULT_EXEMPT_AUTHORS) is False
    assert is_exempt("someone", "Bot", DEFAULT_EXEMPT_AUTHORS) is False


def test_parse_exempt_authors() -> None:
    assert parse_exempt_authors("") == DEFAULT_EXEMPT_AUTHORS
    assert parse_exempt_authors("  a[bot] , b[bot] ") == ("a[bot]", "b[bot]")


# === Output safety ===


def test_error_annotation_is_a_single_escaped_line() -> None:
    """One line, so an embedded workflow command is never at a line start."""
    tampered = body().replace("they pass.", "they pass. 100% ::stop-commands::abcd")
    rendered = render_error(check_body(tampered))
    assert "\n" not in rendered
    assert "\r" not in rendered
    assert rendered.startswith("::error title=Contributor Declaration::")
    assert "%25" in rendered
    assert "100% " not in rendered


def test_error_annotation_strips_control_characters() -> None:
    tampered = body().replace("they pass.", "they pass.\x1b[32m\x1b[2J")
    rendered = render_error(check_body(tampered))
    assert "\x1b" not in rendered


def test_summary_fences_untrusted_lines_that_contain_backticks() -> None:
    """A body cannot break out of the fence and inject markdown or HTML."""
    tampered = body() + '\n```\n<img src="https://example.invalid/beacon.png">\n```\n'
    result = check_body(tampered)
    summary = render_summary(result, DECLARATION_LINES)
    assert "````text" in summary


def test_summary_truncates_a_huge_excerpt() -> None:
    result = check_body(body() + "\n" + "x" * 5000 + "\n")
    summary = render_summary(result, DECLARATION_LINES)
    assert "(truncated)" in summary
    assert "x" * 4000 not in summary


def test_main_only_writes_a_fixed_verdict_to_the_output_file(run: Runner) -> None:
    """A body line reading ``EOF`` must not be able to forge step outputs."""
    code, _, _, output = run(body=body() + "\nEOF\nforged=yes\n")
    assert code == 1
    assert output.splitlines() == ["verdict=not-at-end"]


# === CLI ===


def test_main_passes_on_a_compliant_body(run: Runner) -> None:
    code, out, summary, output = run(body=ORG_TEMPLATE)
    assert code == 0
    assert "::error" not in out
    assert summary == ""
    assert output.splitlines() == ["verdict=ok"]


def test_main_fails_and_writes_a_summary(run: Runner) -> None:
    code, out, summary, output = run(body=body().replace("project's", "project" + chr(0x2019) + "s"))
    assert code == 1
    assert out.startswith("::error title=Contributor Declaration::")
    assert "U+2019" in out
    assert CANONICAL_DECLARATION.strip() in summary
    assert output.splitlines() == ["verdict=diverged"]


@pytest.mark.parametrize(
    ("payload", "code", "verdict"),
    [
        (event(ORG_TEMPLATE), 0, "ok"),
        (event("bump foo from 1 to 2", "dependabot[bot]", "Bot"), 0, "bot-exempt"),
        (event("nothing here", "dependabot[bot]", "User"), 1, "heading-missing"),
    ],
    ids=["body", "bot", "human-with-bot-name"],
)
def test_main_reads_the_event_payload(run: Runner, payload: dict[str, Any], code: int, verdict: str) -> None:
    got_code, _, _, output = run(payload=payload)
    assert got_code == code
    assert output.splitlines() == [f"verdict={verdict}"]


def test_main_uses_the_body_file_and_the_event_file_together(run: Runner) -> None:
    """The API path still needs the payload for the author allowlist."""
    code, _, _, output = run(body="nothing here", payload=event(ORG_TEMPLATE, "dependabot[bot]", "Bot"))
    assert code == 0
    assert output.splitlines() == ["verdict=bot-exempt"]


def test_main_requires_a_source(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2


def test_main_reports_a_missing_body_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--body-file", "/nonexistent/body.md"]) == 2
    assert "::error" in capsys.readouterr().err


def test_main_rejects_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text("{not json", encoding="utf-8")
    assert main(["--event-file", str(event_file)]) == 2
    assert "::error" in capsys.readouterr().err


def test_main_survives_invalid_utf8(run: Runner) -> None:
    code, out, _, output = run(body=b"## Description\n\n\xff\xfe not utf-8\n")
    assert code == 1
    assert output.splitlines() == ["verdict=heading-missing"]
    assert "::error" in out


def test_main_fails_closed_on_an_oversized_body(run: Runner) -> None:
    code, out, _, _ = run(body="x" * (MAX_BODY_CHARS + 1))
    assert code == 1
    assert "limit this check accepts" in out


def test_main_honours_no_summary(run: Runner) -> None:
    code, _, summary, _ = run("--no-summary", body="nothing here")
    assert code == 1
    assert summary == ""


def test_main_honours_a_declaration_file(run: Runner, tmp_path: Path) -> None:
    declaration = tmp_path / "template.md"
    declaration.write_text("### Contributor Declaration\n\nI agree.\n", encoding="utf-8")
    code, _, _, output = run(
        "--declaration-file", str(declaration), body="prose\n\n### Contributor Declaration\n\nI agree.\n"
    )
    assert code == 0
    assert output.splitlines() == ["verdict=ok"]


def test_cli_contract_via_subprocess(tmp_path: Path) -> None:
    """The command line the composite action emits."""
    body_file = tmp_path / "body.md"
    body_file.write_text(ORG_TEMPLATE, encoding="utf-8", newline="")
    completed = subprocess.run(
        [sys.executable, "-m", "ci_infrastructure.check_pr_declaration", "--body-file", str(body_file)],
        cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "::error" not in completed.stdout


# === Shipped YAML and the repo's own template ===


def test_vendored_pr_template_passes() -> None:
    """This repo can never ship a template its own gate rejects."""
    path = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
    with path.open("r", encoding="utf-8", newline="") as handle:
        template = handle.read()
    assert check_body(template).verdict is Verdict.OK


def test_reusable_workflow_job_id_trigger_and_permissions() -> None:
    """The job id is a required-status-check context in every consumer, which all pin ``@main``."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "check-pr-declaration.yml").read_text())
    assert list(workflow["jobs"]) == ["contributor-declaration"]
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    assert workflow["permissions"] == {"contents": "read"}
