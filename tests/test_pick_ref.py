# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""pick-ref's shell script, run for real with a stub `gh` first on PATH.

Same approach as test_require_label_decision.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION = REPO_ROOT / "actions" / "pick-ref" / "action.yml"
FALLBACK = "develop"


def _script() -> str:
    doc: dict[str, Any] = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    (step,) = doc["runs"]["steps"]
    body: str = step["run"]
    return body


def _run(tmp_path: Path, branch: str, *, exists: bool) -> tuple[str, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh_log = tmp_path / "gh-calls.log"
    (bindir / "gh").write_text(f'#!/bin/sh\necho "$*" >> {gh_log}\nexit {0 if exists else 1}\n')
    (bindir / "gh").chmod(0o755)

    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_ACTION_PATH": str(ACTION.parent),
        "GITHUB_OUTPUT": str(outputs),
        "REPO": "ecmwf/eckit",
        "TRY_BRANCH": branch,
        "FALLBACK_REF": FALLBACK,
    }
    proc = subprocess.run(["bash", "-c", _script()], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    (ref,) = (line.removeprefix("ref=") for line in outputs.read_text().splitlines())
    calls = gh_log.read_text().splitlines() if gh_log.exists() else []
    return ref, calls


@pytest.mark.parametrize("branch", ["master", "develop", "try-new-CI", ""])
def test_a_non_sync_branch_is_never_probed(tmp_path: Path, branch: str) -> None:
    """A push to master once built eckit's release `master`, which has no manifest."""
    assert _run(tmp_path, branch, exists=True) == (FALLBACK, [])


@pytest.mark.parametrize("branch", ["feature-sync-foo", "sync-branch-foo"])
def test_a_sync_branch_is_used_where_it_exists(tmp_path: Path, branch: str) -> None:
    ref, calls = _run(tmp_path, branch, exists=True)
    assert ref == branch
    assert calls == [f"api repos/ecmwf/eckit/branches/{branch}"]


def test_a_missing_sync_branch_falls_back(tmp_path: Path) -> None:
    ref, calls = _run(tmp_path, "feature-sync-foo", exists=False)
    assert ref == FALLBACK
    assert len(calls) == 1
