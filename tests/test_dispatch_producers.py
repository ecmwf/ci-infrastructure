# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ci_infrastructure import dispatch_producers
from ci_infrastructure._errors import CIError
from ci_infrastructure.dispatch_producers import main, parse_plans

SHA = "a" * 40
FROM = "b" * 40


def _plan(**over: Any) -> dict[str, Any]:
    return {
        "repo": "ecmwf/eckit",
        "ref": "develop",
        "sha": SHA,
        "lane": "runner",
        "from-sha": FROM,
        "branch": "feature/x",
        "pin": "",
        **over,
    }


def test_plans_are_checked_and_deduplicated() -> None:
    raw = json.dumps([_plan(), _plan(), _plan(lane="hpc-atos"), _plan(**{"rerun-id": 7})])
    got = parse_plans(raw, "ecmwf", ["eckit", "ecmwf/ecbuild"])
    assert [(p.plan.lane, p.plan.rerun_id) for p in got] == [("runner", None), ("hpc-atos", None), ("runner", 7)]
    assert parse_plans("", "ecmwf", ["eckit"]) == []


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"repo": "ecmwf/eccodes"}, "is not one of eckit"),
        ({"repo": "other/eckit"}, "is not one of eckit"),
        ({"lane": "laptop"}, "lane 'laptop'"),
        ({"ref": "--help"}, "is not a ref"),
        ({"ref": "a/../b"}, "is not a ref"),
        ({"branch": "-f"}, "is not a ref"),
        ({"sha": "main"}, "is not a commit"),
        ({"from-sha": "x"}, "is not a commit"),
        ({"rerun-id": "7"}, "is not a run"),
        ({"rerun-id": True}, "is not a run"),
        ({"pin": "ecmwf/eckit@main"}, "--pin must be"),
    ],
)
def test_a_tampered_plan_fails(over: dict[str, Any], message: str) -> None:
    with pytest.raises(CIError, match=message):
        parse_plans(json.dumps([_plan(**over)]), "ecmwf", ["eckit"])


def test_check_outputs_the_repositories_to_mint_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "ecmwf")
    monkeypatch.setenv("DISPATCH_PLANS", json.dumps([_plan(), _plan(repo="ecmwf/ecbuild")]))
    result = CliRunner().invoke(main, ["--repositories", "ecbuild,eckit,eccodes", "--check"])
    assert result.exit_code == 0, result.output
    assert out.read_text() == "repositories=ecbuild,eckit\n"


def test_a_rerun_must_be_that_commits_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def gh(cmd: list[str], token: str | None, **kw: Any) -> tuple[int, str, str]:
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(dispatch_producers, "_gh", gh)
    monkeypatch.setattr(
        dispatch_producers,
        "gh_api_rest",
        lambda path, token: {"head_sha": SHA, "path": ".github/workflows/release.yml"},
    )
    [p] = parse_plans(json.dumps([_plan(**{"rerun-id": 7})]), "ecmwf", ["eckit"])
    with pytest.raises(CIError, match="is not the CI of"):
        dispatch_producers.rerun_producer_ci(plan=p.plan, token=None)
    assert calls == []


def test_dispatch_runs_the_lane_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def gh(cmd: list[str], token: str | None, **kw: Any) -> tuple[int, str, str]:
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(dispatch_producers, "_gh", gh)
    monkeypatch.setattr(dispatch_producers, "_await_run", lambda plan, token: None)
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "ecmwf")
    monkeypatch.setenv("GITHUB_REPOSITORY", "ecmwf/eccodes")
    monkeypatch.setenv("DISPATCH_PLANS", json.dumps([_plan(lane="hpc-atos")]))
    result = CliRunner().invoke(main, ["--repositories", "eckit"])
    assert result.exit_code == 0, result.output
    [cmd] = calls
    assert cmd[:7] == ["gh", "workflow", "run", "cross-repo-trigger-hpc-atos.yml", "--repo", "ecmwf/eckit", "--ref"]
    assert "from-repo=ecmwf/eccodes" in cmd and f"from-sha={FROM}" in cmd and "branch=feature/x" in cmd
