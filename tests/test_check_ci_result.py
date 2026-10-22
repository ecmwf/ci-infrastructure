# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""check_ci_result: the ci-result job's if, action and exact needs."""

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
) -> str:
    return f"""
  ci-result:
    if: {cond}
    needs: {needs}
    runs-on: ubuntu-slim
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
        ({"needs": "pre-commit"}, "is missing: build\n  needs: [build, pre-commit]"),
        ({"needs": "[pre-commit, build, docs]"}, "lists unknown: docs"),
    ],
)
def test_problems(tmp_path: Path, kwargs: dict[str, str], needle: str) -> None:
    [problem] = check(write_ci(tmp_path, JOBS + _result(**kwargs)))
    assert needle in problem


def test_jobs_after_ci_result_are_left_out(tmp_path: Path) -> None:
    after = """
  publish:
    needs: ci-result
    runs-on: ubuntu-slim
    steps: [{run: make publish}]
  announce:
    needs: [publish]
    runs-on: ubuntu-slim
    steps: [{run: echo done}]
"""
    assert check(write_ci(tmp_path, JOBS + _result() + after)) == []


def test_main_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write_ci(tmp_path, JOBS + _result())
    bad = tmp_path / "bad" / "ci.yml"
    bad.parent.mkdir()
    bad.write_text("on: push\njobs:" + JOBS, encoding="utf-8")
    assert main([str(good)]) == 0
    assert main([str(good), str(bad)]) == 1
    assert "no job 'ci-result'" in capsys.readouterr().err
