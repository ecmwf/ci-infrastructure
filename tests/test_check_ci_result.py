# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""check_ci_result: the ci-result job's if, action and needs."""

from __future__ import annotations

from pathlib import Path

import pytest

from ci_infrastructure.check_ci_result import check, main

JOBS = """
  pre-commit:
    runs-on: ubuntu-slim
    steps: [{run: make lint}]
  build:
    needs: pre-commit
    runs-on: ubuntu-slim
    steps: [{run: make}]
"""


def _result(
    needs: str = "[pre-commit, build]",
    cond: str = "always()",
    uses: str = "ecmwf/ci-infrastructure/actions/ci-result@main",
    needs_input: str = "${{ toJSON(needs) }}",
    name: str = "collect results",
    statuses: str = "write",
) -> str:
    return f"""
  ci-result:
    name: {name}
    if: {cond}
    needs: {needs}
    runs-on: ubuntu-slim
    permissions:
      statuses: {statuses}
    steps:
      - uses: {uses}
        with:
          needs: {needs_input}
"""


def write_ci(tmp_path: Path, jobs: str) -> Path:
    p = tmp_path / "ci.yml"
    p.write_text("on: pull_request\njobs:" + jobs, encoding="utf-8")
    return p


def test_complete_job_passes(tmp_path: Path) -> None:
    assert check(write_ci(tmp_path, JOBS + _result())) == []


def test_local_action_and_spacing_pass(tmp_path: Path) -> None:
    assert check(write_ci(tmp_path, JOBS + _result(uses="./actions/ci-result", needs_input="${{toJSON(needs)}}"))) == []


def test_missing_job(tmp_path: Path) -> None:
    [problem] = check(write_ci(tmp_path, JOBS))
    assert "no job 'ci-result'" in problem


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"cond": "success()"}, "if: always()"),
        ({"uses": "actions/checkout@v6"}, "must use ecmwf/ci-infrastructure/actions/ci-result@main"),
        ({"needs_input": "'{}'"}, "must use ecmwf/ci-infrastructure/actions/ci-result@main"),
        ({"needs": "[]"}, "needs no job"),
        ({"name": "ci-result"}, "needs a `name:` other than 'ci-result'"),
        ({"statuses": "read"}, "statuses: write"),
    ],
)
def test_problems(tmp_path: Path, kwargs: dict[str, str], needle: str) -> None:
    [problem] = check(write_ci(tmp_path, JOBS + _result(**kwargs)))
    assert needle in problem


def test_workflow_level_statuses_pass(tmp_path: Path) -> None:
    job = _result().replace("    permissions:\n      statuses: write\n", "")
    p = tmp_path / "ci.yml"
    p.write_text("on: pull_request\npermissions:\n  statuses: write\njobs:" + JOBS + job, encoding="utf-8")
    assert check(p) == []


def test_end_jobs_only_pass(tmp_path: Path) -> None:
    assert check(write_ci(tmp_path, JOBS + _result(needs="build"))) == []


def test_main_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write_ci(tmp_path, JOBS + _result())
    bad = tmp_path / "bad" / "ci.yml"
    bad.parent.mkdir()
    bad.write_text("on: push\njobs:" + JOBS, encoding="utf-8")
    assert main([str(good)]) == 0
    assert main([str(good), str(bad)]) == 1
    assert "no job 'ci-result'" in capsys.readouterr().err
