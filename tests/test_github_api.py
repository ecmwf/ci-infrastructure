# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared GitHub-API and artifact-naming primitives.

`probe_workflow_runs` is the single answer to three questions that used to have
three separate implementations: whether to keep waiting for an artifact
(fetch_deps), whether a missing artifact is explained by a failed build
(check_artifact), and whether a producer is already building (resolve_deps).
None of those copies had a test; these drive the real payload-parsing against
canned REST responses.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from ci_infrastructure import _github_api
from ci_infrastructure._github_api import (
    ManifestSchemaError,
    make_artifact_name,
    probe_workflow_runs,
    resolve_reuse_matrix,
)
from ci_infrastructure.generate_downstream_ci import parse_manifest as generator_parse
from ci_infrastructure.resolve_deps import parse_manifest as resolver_parse

RUNNING = {"status": "in_progress", "html_url": "https://gh/run/1"}
QUEUED = {"status": "queued", "html_url": "https://gh/run/2"}
OK = {"status": "completed", "conclusion": "success"}
FAILED = {"status": "completed", "conclusion": "failure"}
CANCELLED = {"status": "completed", "conclusion": "cancelled"}


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
    # gh_api_rest returns None on a non-zero exit (e.g. a token without
    # actions:read). That must not read as "completed successfully".
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
    # Nothing is decided while a run is still going.
    assert runs.conclusion is None


def test_queued_counts_as_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A queued run has not started, so its output file does not exist yet — but
    the consumer must still wait rather than declare the artifact unbuildable."""
    _payload(monkeypatch, {"workflow_runs": [QUEUED]})
    assert probe_workflow_runs("o/r", "a" * 40, None).detail == "queued"


def test_in_progress_beats_a_failed_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run alongside a live one must not end the wait: the live one may
    still publish. This ordering is why the in-progress scan runs first."""
    _payload(monkeypatch, {"workflow_runs": [FAILED, RUNNING]})
    assert probe_workflow_runs("o/r", "a" * 40, None).state == "running"


def test_all_succeeded_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": [OK, OK]})
    assert probe_workflow_runs("o/r", "a" * 40, None) == ("completed", None, None, "success")


@pytest.mark.parametrize("bad", [FAILED, CANCELLED, {"status": "completed", "conclusion": "timed_out"}])
def test_any_unsuccessful_conclusion_is_failure(monkeypatch: pytest.MonkeyPatch, bad: dict[str, str]) -> None:
    """A cancelled or timed-out producer published nothing, so it must read as a
    failure — otherwise the consumer reports 'built fine but artifact missing'."""
    _payload(monkeypatch, {"workflow_runs": [OK, bad]})
    runs = probe_workflow_runs("o/r", "a" * 40, None)
    assert (runs.state, runs.conclusion) == ("completed", "failure")
    assert not runs.in_flight


def test_non_dict_entries_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _payload(monkeypatch, {"workflow_runs": ["nonsense", None, OK]})
    assert probe_workflow_runs("o/r", "a" * 40, None).conclusion == "success"


# --------------------------------------------------------------------------- #
# make_artifact_name — the format IS the cache key, so it is pinned literally.
# --------------------------------------------------------------------------- #
SHA = "a" * 40


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
    """Every optional segment vanishes rather than leaving an empty one — a
    doubled hyphen would be a different (and permanently unresolvable) key."""
    assert make_artifact_name("pkg", SHA, deps_hash8, "ubuntu-24.04", compiler, "Release", python_version) == expected


# --------------------------------------------------------------------------- #
# resolve_reuse_matrix — the generator and the resolver must agree on legs
# --------------------------------------------------------------------------- #
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
    """The caller stores these per kind; a shared dict would let an edit to one
    kind's leg silently change another's."""
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


def test_generator_and_resolver_expand_reuse_matrix_identically(tmp_path: Path) -> None:
    """The invariant behind sharing this: a kind whose legs differed between the
    two parsers would have the resolver look up artifact names for legs the
    generator never emitted a job for (or the reverse), and every lookup misses.
    """
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

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        cxx-compiler = "clang++-18"
        build-type = "Release"
        platform = "ubuntu-24.04"
    """)
    manifest_path = tmp_path / "r" / ".ci" / "manifest.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(body)

    generated = generator_parse(manifest_path)
    resolved = resolver_parse(body)

    for kind in ("build", "test"):
        assert [dict(leg) for leg in generated.matrices[kind].legs] == resolved.matrix[kind]
    # …and reuse really did inherit rather than yield nothing.
    assert resolved.matrix["test"] == resolved.matrix["build"] != []
