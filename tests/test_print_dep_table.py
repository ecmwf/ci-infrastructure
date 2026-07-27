"""Tests for the resolved-dependencies Markdown table.

The columns are read straight from the resolver's structured fields — platform /
compiler / python / build-type / deps-hash — instead of reverse-parsing the
artifact name. These tests pin that every column lands in its own cell for the
shapes that the old positional parser mis-typeset (compiler-less packages, where
`build-type` used to bleed into the Platform column), and that the Ref column
shows a branch but blanks a pinned SHA.
"""

from __future__ import annotations

import pytest

from ci_infrastructure.print_dep_table import _looks_like_sha, _md_table, _row_from_dep

SHA = "a" * 40


def _dep(**overrides: str) -> dict[str, str]:
    base = {
        "name": "pkg",
        "repo": "owner/pkg",
        "ref": "main",
        "sha": SHA,
        "artifact-name": "pkg-...",
        "platform": "ubuntu-24.04",
        "compiler": "",
        "build-type": "Release",
        "python-version": "",
        "deps-hash": "",
        "source": "artifact",
    }
    base.update(overrides)
    return base


def test_compiler_less_dep_keeps_build_type_in_its_column() -> None:
    # The ecbuild case: no compiler segment. build-type must NOT bleed into platform.
    row = _row_from_dep(_dep(compiler=""))
    assert row["platform"] == "ubuntu-24.04"
    assert row["build_type"] == "Release"
    assert row["compiler"] == ""
    assert row["python"] == ""


def test_compiler_less_python_dep_splits_all_columns() -> None:
    # Pure-Python package: platform + python + build-type each in their own cell.
    row = _row_from_dep(_dep(compiler="", **{"python-version": "3.11"}))
    assert row["platform"] == "ubuntu-24.04"
    assert row["python"] == "3.11"
    assert row["build_type"] == "Release"
    assert row["compiler"] == ""


def test_full_dep_regression() -> None:
    row = _row_from_dep(_dep(compiler="clang++-18", **{"deps-hash": "abc12345", "build-type": "Debug"}))
    assert row["compiler"] == "clang++-18"
    assert row["deps_hash"] == "abc12345"
    assert row["build_type"] == "Debug"
    assert row["platform"] == "ubuntu-24.04"


def test_ref_column_shows_branch_but_blanks_pinned_sha() -> None:
    assert _row_from_dep(_dep(ref="main"))["ref"] == "main"
    assert _row_from_dep(_dep(ref="release-1.x"))["ref"] == "release-1.x"
    assert _row_from_dep(_dep(ref=SHA))["ref"] == ""
    assert _row_from_dep(_dep(ref="abc1234"))["ref"] == ""


def test_looks_like_sha() -> None:
    assert _looks_like_sha("a" * 40)
    assert _looks_like_sha("abc1234")
    assert not _looks_like_sha("main")
    assert not _looks_like_sha("release-1.x")
    assert not _looks_like_sha("")  # empty ref → not a SHA → shown as blank anyway


def test_md_table_has_ref_as_second_column() -> None:
    rows = [_row_from_dep(_dep())]
    table = _md_table(rows, show_source=False)
    header = table.splitlines()[0]
    cells = [c.strip() for c in header.strip("|").split("|")]
    assert cells[:3] == ["Package", "Ref", "SHA"]
    assert "Release" in table


def test_main_renders_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from click.testing import CliRunner

    from ci_infrastructure import print_dep_table

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    deps = [_dep(name="ecbuild", repo="owner/ecbuild", compiler="")]
    result = CliRunner().invoke(print_dep_table.main, ["--deps-json", json.dumps(deps)])
    assert result.exit_code == 0
    out = result.output
    assert "| Package" in out and "| Ref" in out and "| Build type" in out
    # build-type sits under its own header, not merged into platform
    assert "ubuntu-24.04-Release" not in out


def test_main_orders_own_first_then_deps_downstream_to_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from click.testing import CliRunner

    from ci_infrastructure import print_dep_table

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # deps-json arrives upstream→downstream (the build's link order).
    deps = [_dep(name="A"), _dep(name="B"), _dep(name="E")]
    own = _dep(name="D")
    result = CliRunner().invoke(print_dep_table.main, ["--deps-json", json.dumps(deps), "--own", json.dumps(own)])
    assert result.exit_code == 0
    out = result.output
    # OWN on top, then deps reversed to downstream→upstream: D, E, B, A.
    positions = [out.index(f"[{name}]") for name in ("D", "E", "B", "A")]
    assert positions == sorted(positions)
