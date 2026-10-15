# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""resolve-dispatch-context's `run:` body, executed with a stub `gh` answering by API path."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

from conftest import action_run_body, stub_gh

REPO: Final = "ecmwf/eccodes"
FORK: Final = "someone/eccodes"
SHA: Final = "a" * 40
BASE_SHA: Final = "b" * 40
FORK_CI: Final = "ci-fork.yml"


def _pr(*, head_repo: str = FORK, head_sha: str = SHA, state: str = "open", number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "head": {"sha": head_sha, "ref": "sync-branch/anything", "repo": {"full_name": head_repo}},
        "base": {"ref": "develop"},
    }


def _fork_run(title: str = f"CI (fork) #7 @ {SHA}", conclusion: str = "success") -> dict[str, Any]:
    return {"id": 99, "conclusion": conclusion, "html_url": "https://run/99", "display_title": title}


def _ctx(
    tmp_path: Path,
    event_name: str,
    event: dict[str, Any],
    *,
    pr: dict[str, Any] | None = None,
    runs: list[dict[str, Any]] | None = None,
    fork_runs: list[dict[str, Any]] | None = None,
    approval: str = "success",
    fork_ci: str = FORK_CI,
    pull_request: str = "",
) -> tuple[int, dict[str, str], str]:
    files = {
        "pr": pr or {},
        "runs": {"workflow_runs": runs or []},
        "fork-runs": {"workflow_runs": fork_runs or []},
        "jobs": {
            "jobs": [
                {"name": "ci / pre-commit", "conclusion": "success"},
                {"name": "ci / ci-approval", "conclusion": approval},
            ]
        },
        "event": event,
    }
    for name, body in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(body))
    gh = (
        'case "$*" in\n'
        f"  */jobs*) cat {tmp_path}/jobs.json ;;\n"
        f"  */actions/workflows/*) cat {tmp_path}/fork-runs.json ;;\n"
        f"  */actions/runs*) cat {tmp_path}/runs.json ;;\n"
        f"  */pulls/*) cat {tmp_path}/pr.json ;;\n"
        '  *) echo unexpected gh call: "$*" >&2; exit 2 ;;\n'
        "esac"
    )
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
        "RUNNER_TEMP": str(tmp_path),
        "CI_WORKFLOW_INPUT": "CI",
        "FORK_CI_WORKFLOW_INPUT": fork_ci,
        "PULL_REQUEST_INPUT": pull_request,
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("resolve-dispatch-context")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines()) if outputs.exists() else {}
    return proc.returncode, out, proc.stderr + proc.stdout


def _workflow_run(
    *, path: str, head_repo: str, head_sha: str, title: str = "x", conclusion: str = "success"
) -> dict[str, Any]:
    return {
        "workflow_run": {
            "path": f".github/workflows/{path}",
            "head_sha": head_sha,
            "head_branch": "develop",
            "head_repository": {"full_name": head_repo},
            "conclusion": conclusion,
            "html_url": "https://run/1",
            "display_title": title,
        }
    }


def test_a_ci_run_of_this_repo_passes_its_verdict_through(tmp_path: Path) -> None:
    code, out, _ = _ctx(tmp_path, "workflow_run", _workflow_run(path="ci.yml", head_repo=REPO, head_sha=SHA))
    assert code == 0
    assert (out["head-sha"], out["head-branch"], out["ci-conclusion"], out["pull-request"]) == (
        SHA,
        "develop",
        "success",
        "",
    )


def test_the_plain_ci_run_of_a_fork_has_no_verdict(tmp_path: Path) -> None:
    """Its jobs were all skipped, which can still conclude success."""
    code, out, log = _ctx(tmp_path, "workflow_run", _workflow_run(path="ci.yml", head_repo=FORK, head_sha=SHA))
    assert code == 0
    assert out["ci-conclusion"] == ""
    assert "nothing to do" in log


def test_an_approved_fork_run_names_the_pr_its_commit_and_the_base_branch(tmp_path: Path) -> None:
    event = _workflow_run(path=FORK_CI, head_repo=REPO, head_sha=BASE_SHA, title=f"CI (fork) #7 @ {SHA}")
    code, out, _ = _ctx(tmp_path, "workflow_run", event, pr=_pr(), fork_runs=[_fork_run()])
    assert code == 0
    assert (out["head-sha"], out["head-branch"], out["ci-conclusion"], out["pull-request"]) == (
        SHA,
        "develop",
        "success",
        "7",
    )


def test_a_fork_run_whose_gate_did_not_pass_is_not_an_approval(tmp_path: Path) -> None:
    event = _workflow_run(path=FORK_CI, head_repo=REPO, head_sha=BASE_SHA, title=f"CI (fork) #7 @ {SHA}")
    code, out, _ = _ctx(tmp_path, "workflow_run", event, pr=_pr(), fork_runs=[_fork_run()], approval="skipped")
    assert code == 0
    assert (out["ci-conclusion"], out["ci-summary"]) == ("", "No approved CI run for this commit")


def test_a_fork_run_for_a_commit_the_pr_moved_on_from_does_nothing(tmp_path: Path) -> None:
    event = _workflow_run(path=FORK_CI, head_repo=REPO, head_sha=BASE_SHA, title=f"CI (fork) #7 @ {SHA}")
    code, out, log = _ctx(tmp_path, "workflow_run", event, pr=_pr(head_sha="c" * 40), fork_runs=[_fork_run()])
    assert code == 0
    assert out["ci-conclusion"] == ""
    assert "moved on" in log


def test_a_dispatch_for_a_pr_of_this_repo_reads_its_ci_run(tmp_path: Path) -> None:
    runs = [{"name": "CI", "conclusion": "success", "html_url": "https://run/5"}]
    code, out, _ = _ctx(
        tmp_path, "workflow_dispatch", {}, pr=_pr(head_repo=REPO, number=3), runs=runs, pull_request="3"
    )
    assert code == 0
    assert (out["head-sha"], out["head-branch"], out["ci-conclusion"], out["pull-request"]) == (
        SHA,
        "sync-branch/anything",
        "success",
        "3",
    )


def test_a_dispatch_for_a_fork_needs_an_approved_fork_run(tmp_path: Path) -> None:
    code, out, _ = _ctx(tmp_path, "workflow_dispatch", {}, pr=_pr(), fork_runs=[], pull_request="7")
    assert code == 0
    assert (out["ci-conclusion"], out["head-branch"]) == ("", "develop")


def test_a_fork_pr_counts_for_nothing_without_the_opt_in(tmp_path: Path) -> None:
    code, out, _ = _ctx(tmp_path, "workflow_dispatch", {}, pr=_pr(), fork_ci="", pull_request="7")
    assert code == 0
    assert (out["ci-conclusion"], out["ci-summary"]) == ("", "Fork pull requests are not enabled")


def test_a_dispatch_without_a_pr_number_fails(tmp_path: Path) -> None:
    code, _, log = _ctx(tmp_path, "workflow_dispatch", {}, pull_request="develop")
    assert code == 1
    assert "needs the pull request number" in log
