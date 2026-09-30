# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""check-pr-label's `run:` body, executed with a stub `gh` returning the commit's pull requests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest
from conftest import action_run_body, stub_gh

LABEL: Final = "run-downstream-ci"


def _gate(tmp_path: Path, labels: list[str] | None) -> tuple[int, dict[str, str], str]:
    """`labels=None` is a push: the commit has no open pull request."""
    pulls = [] if labels is None else [{"state": "open", "labels": [{"name": n} for n in labels]}]
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
        ["bash", "-c", action_run_body("check-pr-label")], env=env, capture_output=True, text=True, check=False
    )
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines()) if outputs.exists() else {}
    return proc.returncode, out, proc.stderr


@pytest.mark.parametrize(
    ("labels", "run", "depth"),
    [(None, "true", "all"), ([f"{LABEL}:all"], "true", "all"), ([f"{LABEL}:2"], "true", "2"), ([], "false", "")],
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
