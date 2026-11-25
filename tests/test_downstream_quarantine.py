# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import REPO_ROOT, action_run_body, stub_gh

from ci_infrastructure._errors import CIError
from ci_infrastructure.downstream_quarantine import Quarantined, judge, load, main

QUARANTINE = {"geo": Quarantined("gridspec tests fail.", datetime.date(2026, 11, 25))}
JOBS: dict[str, str | None] = {"validate": None, "geo": "ecmwf/geo", "plots": "ecmwf/plots"}


@pytest.mark.parametrize(
    ("results", "state", "ignored"),
    [
        ({"validate": "success", "geo": "success", "plots": "success"}, "success", {}),
        ({"validate": "success", "geo": "failure", "plots": "skipped"}, "success", {"geo": "failure"}),
        ({"validate": "success", "geo": "cancelled", "plots": "success"}, "success", {"geo": "cancelled"}),
        ({"validate": "success", "geo": "failure", "plots": "failure"}, "failure", {"geo": "failure"}),
        ({"validate": "failure", "geo": "success", "plots": "success"}, "failure", {}),
        ({"validate": "success", "geo": "skipped", "plots": "skipped"}, "success", {}),
    ],
    ids=["green", "quarantined-fails", "quarantined-cancelled", "other-fails-too", "validate", "skipped"],
)
def test_judge_ignores_only_quarantined_consumers(results: dict[str, str], state: str, ignored: dict[str, str]) -> None:
    verdict = judge(results, JOBS, QUARANTINE)
    assert (verdict.state, verdict.ignored) == (state, ignored)


def test_judge_needs_a_result_for_every_judged_job() -> None:
    with pytest.raises(CIError, match="no result for plots"):
        judge({"validate": "success", "geo": "success"}, JOBS, QUARANTINE)


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"repos": {"geo": {"reason": "x"}}}, "since is required"),
        ({"repos": {"geo": {"since": datetime.date(2026, 11, 25)}}}, "reason is required"),
        ({"repos": {"geo": {"reason": "x", "since": "2026-11-25"}}}, "since is required, as a date"),
        ({"repos": {"geo": {"reason": "x", "since": datetime.date(2026, 11, 25), "until": 1}}}, "unknown keys until"),
        ({"repo": {}}, "unknown tables repo"),
    ],
)
def test_load_rejects_malformed_entries(config: dict[str, Any], match: str) -> None:
    with pytest.raises(CIError, match=match):
        load(config)


def test_the_committed_quarantine_names_rolled_out_repos() -> None:
    quarantine = load(tomllib.loads((REPO_ROOT / ".github/downstream-quarantine.toml").read_text(encoding="utf-8")))
    rolled_out = tomllib.loads((REPO_ROOT / ".github/rolled-out-repos.toml").read_text(encoding="utf-8"))["repos"]
    assert set(quarantine) <= set(rolled_out)


def _cli(tmp_path: Path, results: dict[str, str], quarantine: str | None) -> tuple[str, str, str]:
    path = tmp_path / "quarantine.toml"
    if quarantine is not None:
        path.write_text(quarantine)
    out, summary = tmp_path / "out", tmp_path / "summary"
    needs = {jid: {"result": r, "outputs": {}} for jid, r in results.items()}
    proc = CliRunner().invoke(
        main,
        ["--results", json.dumps(needs), "--jobs", json.dumps(JOBS), "--quarantine", str(path)],
        env={"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)},
    )
    assert proc.exit_code == 0, proc.output
    return out.read_text(), summary.read_text() if summary.exists() else "", proc.output


def test_cli_reports_the_ignored_failure(tmp_path: Path) -> None:
    out, summary, log = _cli(
        tmp_path,
        {"ci-approval": "success", "validate": "success", "geo": "failure", "plots": "skipped"},
        '[repos.geo]\nreason = "gridspec tests fail."\nsince = 2026-11-25\n',
    )
    assert "state=success\n" in out and "ignored=geo\n" in out
    assert "- `geo` (failure): gridspec tests fail. Quarantined since 2026-11-25." in summary
    assert "::notice title=Downstream quarantine::geo failure" in log


def test_cli_without_a_quarantine_file_ignores_nothing(tmp_path: Path) -> None:
    out, summary, _ = _cli(tmp_path, {"validate": "success", "geo": "failure", "plots": "skipped"}, None)
    assert "state=failure\n" in out and "ignored=\n" in out
    assert summary == ""


@pytest.mark.parametrize(
    ("scope", "ignored", "description"),
    [
        ("all", "", "Downstream runner tests success"),
        ("2", "", "Downstream runner tests success (up to level 2)"),
        ("all", "geo plots", "Downstream runner tests success; quarantined: geo, plots"),
    ],
)
def test_action_posts_the_state_with_scope_and_quarantine(
    tmp_path: Path, scope: str, ignored: str, description: str
) -> None:
    log = tmp_path / "gh.log"
    bindir = stub_gh(tmp_path, f'printf "%s\\n" "$@" > {log}')
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "ecmwf/up",
        "STATE": "success",
        "IGNORED": ignored,
        "CONTEXT": "downstream/runner",
        "SHA": "abc123",
        "DESCRIPTION": "Downstream runner tests",
        "SCOPE": scope,
        "TARGET_URL": "https://example/run",
    }
    body = action_run_body("report-downstream-result", "post")
    subprocess.run(["bash", "-c", body], env=env, check=True)
    args = log.read_text().splitlines()
    assert "/repos/ecmwf/up/statuses/abc123" in args
    assert f"description={description}" in args
    assert "context=downstream/runner" in args
