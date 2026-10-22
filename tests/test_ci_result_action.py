# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""ci-result's `run:` body against needs contexts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import action_run_body


def _run(tmp_path: Path, needs: str) -> tuple[int, str, str]:
    summary = tmp_path / "summary.md"
    env = {**os.environ, "NEEDS": needs, "GITHUB_STEP_SUMMARY": str(summary)}
    proc = subprocess.run(
        ["bash", "-c", action_run_body("ci-result")], env=env, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr, summary.read_text() if summary.exists() else ""


def _needs(**results: str) -> str:
    return json.dumps({job: {"result": r, "outputs": {}} for job, r in results.items()})


def test_success_and_skipped_pass(tmp_path: Path) -> None:
    code, out, summary = _run(tmp_path, _needs(build="success", publish="skipped"))
    assert code == 0, out
    assert "| build | success |" in summary
    assert "| publish | skipped |" in summary


@pytest.mark.parametrize("bad", ["failure", "cancelled"])
def test_failed_job_fails(tmp_path: Path, bad: str) -> None:
    code, out, _ = _run(tmp_path, _needs(build="success", test=bad))
    assert code == 1
    assert f"::error title=CI result::test ({bad})" in out


def test_all_skipped_fails(tmp_path: Path) -> None:
    code, out, _ = _run(tmp_path, _needs(**{"ci-approval": "skipped", "build": "skipped"}))
    assert code == 1
    assert "no job ran" in out


@pytest.mark.parametrize("needs", ["", "{}", "[]", "not json"])
def test_missing_needs_fails(tmp_path: Path, needs: str) -> None:
    code, out, _ = _run(tmp_path, needs)
    assert code == 1
    assert "toJSON(needs)" in out
