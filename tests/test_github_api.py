# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""probe_workflow_runs, make_artifact_name and resolve_reuse_matrix, against canned REST payloads."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import write_repo

from ci_infrastructure import _github_api
from ci_infrastructure._github_api import (
    ManifestSchemaError,
    make_artifact_name,
    probe_workflow_runs,
    resolve_reuse_matrix,
    template_version_for_lane,
)
from ci_infrastructure.generate_downstream_ci import parse_manifest as generator_parse
from ci_infrastructure.resolve_deps import parse_manifest as resolver_parse

RUNNING: Final = {"status": "in_progress", "html_url": "https://gh/run/1"}
QUEUED: Final = {"status": "queued", "html_url": "https://gh/run/2"}
OK: Final = {"status": "completed", "conclusion": "success"}
FAILED: Final = {"status": "completed", "conclusion": "failure"}
CANCELLED: Final = {"status": "completed", "conclusion": "cancelled"}


def _payload(monkeypatch: pytest.MonkeyPatch, data: Any) -> None:
    """Stub only the HTTP boundary; all parsing under test runs for real."""
    monkeypatch.setattr(_github_api, "gh_api_rest", lambda path, token: data)


def test_no_runs_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": []})
    assert probe_workflow_runs("o/r", "a" * 40, None).state == "none"


def test_absent_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {})
    assert probe_workflow_runs("o/r", "a" * 40, None).state == "none"


def test_failed_api_call_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # gh_api_rest returns None on a non-zero exit.
    _payload(monkeypatch, None)
    runs = probe_workflow_runs("o/r", "a" * 40, None)
    assert runs.state == "none"
    assert runs.conclusion is None


def test_in_progress_run_reports_detail_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": [OK, RUNNING]})
    runs = probe_workflow_runs("o/r", "a" * 40, None)
    assert runs.state == "running"
    assert runs.in_flight
    assert runs.detail == "in_progress"
    assert runs.url == "https://gh/run/1"
    assert runs.conclusion is None


def test_queued_counts_as_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": [QUEUED]})
    assert probe_workflow_runs("o/r", "a" * 40, None).detail == "queued"


def test_in_progress_beats_a_failed_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live run may still publish."""
    _payload(monkeypatch, {"workflow_runs": [FAILED, RUNNING]})
    assert probe_workflow_runs("o/r", "a" * 40, None).state == "running"


def test_all_succeeded_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": [OK, OK]})
    assert probe_workflow_runs("o/r", "a" * 40, None) == ("completed", None, None, "success")


@pytest.mark.parametrize("bad", [FAILED, CANCELLED, {"status": "completed", "conclusion": "timed_out"}])
def test_any_unsuccessful_conclusion_is_failure(monkeypatch: pytest.MonkeyPatch, bad: dict[str, str]) -> None:
    _payload(monkeypatch, {"workflow_runs": [OK, bad]})
    runs = probe_workflow_runs("o/r", "a" * 40, None)
    assert (runs.state, runs.conclusion) == ("completed", "failure")
    assert not runs.in_flight


def test_non_dict_entries_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": ["nonsense", None, OK]})
    assert probe_workflow_runs("o/r", "a" * 40, None).conclusion == "success"


# === make_artifact_name — the format IS the cache key, so it is pinned literally. ===
SHA: Final = "a" * 40


def test_artifact_name_full_shape() -> None:
    assert make_artifact_name("pkg", SHA, "abc12345", "ubuntu-24.04", "clang++-18", "Release", "3.12", "moments") == (
        f"pkg-{SHA}-abc12345-ubuntu-24.04-clang++-18-py3.12-Release-opts.moments"
    )


@pytest.mark.parametrize(
    ("deps_hash8", "compiler", "python_version", "expected"),
    [
        (None, None, None, f"pkg-{SHA}-ubuntu-24.04-Release"),
        ("abc12345", None, None, f"pkg-{SHA}-abc12345-ubuntu-24.04-Release"),
        (None, "clang++-18", None, f"pkg-{SHA}-ubuntu-24.04-clang++-18-Release"),
        (None, None, "3.11", f"pkg-{SHA}-ubuntu-24.04-py3.11-Release"),
    ],
)
def test_absent_segments_are_dropped_not_blanked(
    deps_hash8: str | None, compiler: str | None, python_version: str | None, expected: str
) -> None:
    assert make_artifact_name("pkg", SHA, deps_hash8, "ubuntu-24.04", compiler, "Release", python_version) == expected


def test_template_version_follows_the_build_type() -> None:
    assert (
        make_artifact_name("pkg", SHA, None, "hpc-atos-gnu", "g++-8", "Release", None, "moments", template_version=2)
        == f"pkg-{SHA}-hpc-atos-gnu-g++-8-Release-hpcv2-opts.moments"
    )
    assert make_artifact_name("pkg", SHA, None, "hpc-atos-gnu", None, "Release", None, template_version=0) == (
        f"pkg-{SHA}-hpc-atos-gnu-Release"
    )


def test_only_the_hpc_lane_carries_the_template_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_github_api, "HPC_TEMPLATE_VERSION", 3)
    assert template_version_for_lane("hpc") == 3
    assert template_version_for_lane("runner") == 0


# === resolve_reuse_matrix — the generator and the resolver must agree on legs ===
_BLOCKS: dict[str, dict[str, Any]] = {
    "build": {"include": [{"cxx-compiler": "clang++-18"}]},
    "test": {"reuse-matrix": "build"},
    "chained": {"reuse-matrix": "test"},
}


def test_reuse_matrix_inherits_the_target_legs() -> None:
    assert resolve_reuse_matrix("test", None, "build", _BLOCKS) == ({"cxx-compiler": "clang++-18"},)


def test_explicit_include_is_used_verbatim() -> None:
    assert resolve_reuse_matrix("build", [{"a": "1"}], None, _BLOCKS) == ({"a": "1"},)


def test_a_kind_with_neither_has_no_legs() -> None:
    assert resolve_reuse_matrix("bare", None, None, _BLOCKS) == ()


def test_legs_are_copied_not_aliased() -> None:
    legs = resolve_reuse_matrix("test", None, "build", _BLOCKS)
    legs[0]["cxx-compiler"] = "mutated"
    assert _BLOCKS["build"]["include"][0] == {"cxx-compiler": "clang++-18"}


@pytest.mark.parametrize(
    ("kind", "include", "reuse", "expected"),
    [
        ("test", [{"a": "1"}], "build", "both"),
        ("t", None, "nope", "does not exist"),
        ("chained", None, "test", "chained reuse is not supported"),
        ("bad", "not-a-list", None, "must be an array of tables"),
    ],
)
def test_reuse_matrix_rejections(kind: str, include: Any, reuse: Any, expected: str) -> None:
    with pytest.raises(ManifestSchemaError, match=expected):
        resolve_reuse_matrix(kind, include, reuse, _BLOCKS)


def test_defaults_fill_in_under_each_leg() -> None:
    blocks = {"build": {"defaults": {"build-type": "RelWithDebInfo", "ntasks": 2}}}
    legs = resolve_reuse_matrix("build", [{"a": "1"}, {"a": "2", "ntasks": 4}], None, blocks)
    assert legs == (
        {"build-type": "RelWithDebInfo", "ntasks": 2, "a": "1"},
        {"build-type": "RelWithDebInfo", "ntasks": 4, "a": "2"},
    )


def test_reuse_takes_the_target_defaults_then_its_own() -> None:
    blocks: dict[str, dict[str, Any]] = {
        "build": {"include": [{"a": "1"}], "defaults": {"build-type": "Release", "tests": True}},
        "test": {"reuse-matrix": "build", "defaults": {"build-type": "Debug", "extra": "x"}},
    }
    assert resolve_reuse_matrix("test", None, "build", blocks) == (
        {"extra": "x", "build-type": "Release", "tests": True, "a": "1"},
    )


def test_defaults_must_be_a_table() -> None:
    with pytest.raises(ManifestSchemaError, match=r"\[matrix.build.defaults\] must be a table"):
        resolve_reuse_matrix("build", [{"a": "1"}], None, {"build": {"defaults": "x"}})


def test_generator_and_resolver_expand_reuse_matrix_identically(tmp_path: Path) -> None:
    body = textwrap.dedent("""
        [package]
        name = "a"
        prefix = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.build]
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a"

        [matrix.test]
        reuse-matrix = "build"
        triggers = ["upstream-change"]
        action = "./.github/actions/test-a"
        publishes = false

        [matrix.build.defaults]
        build-type = "Release"

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        cxx-compiler = "clang++-18"
        platform = "ubuntu-24.04"
    """)
    manifest_path = write_repo(tmp_path, "a", body)

    generated = generator_parse(manifest_path)
    resolved = resolver_parse(manifest_path.read_text())

    for kind in ("build", "test"):
        assert [dict(leg) for leg in generated.matrices[kind].legs] == resolved.matrix[kind]
    assert resolved.matrix["test"] == resolved.matrix["build"] != []
    assert resolved.matrix["build"][0]["build-type"] == "Release"
