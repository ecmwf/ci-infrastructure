# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""should-downstream-run's `run:` body, executed with a stub `gh` returning the commit's pull requests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest
from conftest import action_run_body, stub_gh

LABEL: Final = "run-downstream-ci"


def _pull(head_sha: str, labels: list[str]) -> dict[str, object]:
    return {"state": "open", "head": {"sha": head_sha}, "labels": [{"name": n} for n in labels]}


def _gate(tmp_path: Path, labels: list[str] | None) -> tuple[int, dict[str, str], str]:
    """`labels=None` is a push: the commit has no open pull request."""
    pulls = [] if labels is None else [_pull("abc123", labels)]
    payload = tmp_path / "pulls.json"
    payload.write_text(json.dumps(pulls))
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, f'cat {payload}')}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_REPOSITORY": "ecmwf/ecbuild",
        "RUNNER_TEMP": str(tmp_path),
        "LABEL_INPUT": LABEL,
        "SHA_INPUT": "abc123",
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("should-downstream-run")], env=env, capture_output=True, text=True, check=False
    )
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines()) if outputs.exists() else {}
    return proc.returncode, out, proc.stderr


@pytest.mark.parametrize(
    ("labels", "run", "depth"),
    [(None, "false", ""), ([f"{LABEL}:all"], "true", "all"), ([f"{LABEL}:2"], "true", "2"), ([], "false", "")],
    ids=["push", "all", "level", "none"],
)
def test_the_level_label_sets_run_and_depth(tmp_path: Path, labels: list[str] | None, run: str, depth: str) -> None:
    code, out, _ = _gate(tmp_path, labels)
    assert code == 0
    assert (out["run"], out["depth"]) == (run, depth)


@pytest.mark.parametrize(
    "labels",
    [[LABEL], [f"{LABEL}:all", f"{LABEL}:1"], [f"{LABEL}:0"], [f"{LABEL}:abc"]],
    ids=["bare", "two", "zero", "word"],
)
def test_anything_but_exactly_one_level_fails(tmp_path: Path, labels: list[str]) -> None:
    code, _, err = _gate(tmp_path, labels)
    assert code == 1
    assert "set exactly one of 'run-downstream-ci:all' or 'run-downstream-ci:<n>'" in err


def test_other_labels_are_ignored(tmp_path: Path) -> None:
    code, out, _ = _gate(tmp_path, ["documentation", f"{LABEL}-extra", f"{LABEL}:1"])
    assert (code, out["run"], out["depth"]) == (0, "true", "1")


def _run_gate(tmp_path: Path, gh_output: object, **inputs: str) -> tuple[int, dict[str, str], str]:
    payload = tmp_path / "gh.json"
    payload.write_text(json.dumps(gh_output))
    outputs = tmp_path / "outputs.txt"
    gh = 'echo "$*" > ' + f"{tmp_path}/call; cat {payload}"
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_REPOSITORY": "ecmwf/ecbuild",
        "RUNNER_TEMP": str(tmp_path),
        "LABEL_INPUT": LABEL,
        "SHA_INPUT": "abc123",
        **inputs,
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("should-downstream-run")], env=env, capture_output=True, text=True, check=False
    )
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines()) if outputs.exists() else {}
    return proc.returncode, out, proc.stderr


@pytest.mark.parametrize("level", ["all", "2"])
def test_a_dispatched_level_replaces_the_label(tmp_path: Path, level: str) -> None:
    code, out, _ = _run_gate(tmp_path, [], LEVEL_INPUT=level)
    assert (code, out["run"], out["depth"]) == (0, "true", level)
    assert not (tmp_path / "call").exists()


def test_a_dispatched_level_must_be_a_level(tmp_path: Path) -> None:
    code, _, err = _run_gate(tmp_path, [], LEVEL_INPUT="0")
    assert code == 1
    assert "level must be 'all' or a level from 1" in err


def test_a_named_pull_request_is_read_directly(tmp_path: Path) -> None:
    """A fork's commit is not one `commits/<sha>/pulls` finds."""
    pr = {"state": "open", "labels": [{"name": f"{LABEL}:1"}]}
    code, out, _ = _run_gate(tmp_path, pr, PULL_REQUEST_INPUT="7")
    assert (code, out["run"], out["depth"]) == (0, "true", "1")
    assert "/repos/ecmwf/ecbuild/pulls/7" in (tmp_path / "call").read_text()


def test_a_pull_request_stacked_on_the_commit_lends_no_label(tmp_path: Path) -> None:
    """`commits/<sha>/pulls` also lists open PRs whose branch merely contains the commit."""
    pulls = [_pull("abc123", ["downstream-ci-not-needed"]), _pull("def456", [f"{LABEL}:all"])]
    code, out, _ = _run_gate(tmp_path, pulls)
    assert (code, out["run"], out["depth"]) == (0, "false", "")


@pytest.mark.parametrize(("labels", "run"), [([f"{LABEL}:1", "run-hpc-ci"], "true"), ([f"{LABEL}:1"], "false")])
def test_a_required_label_must_be_there_too(tmp_path: Path, labels: list[str], run: str) -> None:
    code, out, _ = _run_gate(tmp_path, [_pull("abc123", labels)], REQUIRE_LABEL_INPUT="run-hpc-ci")
    assert (code, out["run"]) == (0, run)


@pytest.mark.parametrize(("labels", "run"), [(["run-hpc-ci"], "true"), ([], "false")])
def test_a_dispatched_level_still_needs_the_required_label(tmp_path: Path, labels: list[str], run: str) -> None:
    pr = {"state": "open", "labels": [{"name": n} for n in labels]}
    code, out, _ = _run_gate(tmp_path, pr, LEVEL_INPUT="all", PULL_REQUEST_INPUT="7", REQUIRE_LABEL_INPUT="run-hpc-ci")
    assert (code, out["run"]) == (0, run)
