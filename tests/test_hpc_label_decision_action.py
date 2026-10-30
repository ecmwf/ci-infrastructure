# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""hpc-label-decision's `run:` body, with a stub `gh` answering by API path and logging every call."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from conftest import REPO_ROOT, stub_gh

REPO: Final = "ecmwf/eckit"
FORK: Final = "someone/eckit"
SHA: Final = "a" * 40
LABEL: Final = "run-hpc-ci"
OPT_OUT: Final = "hpc-ci-not-needed"
CONTEXT: Final = "ci-result/hpc-atos"


def _body() -> str:
    doc: dict[str, Any] = yaml.safe_load((REPO_ROOT / "actions/hpc-label-decision/action.yml").read_text())
    return str(next(s for s in doc["runs"]["steps"] if s.get("id") == "decide")["run"])


def _jobs(gate: str = "success", collect: str = "skipped", prefix: str = "") -> list[dict[str, Any]]:
    return [
        {"id": 11, "name": f"{prefix}hpc-gate", "conclusion": gate},
        {"id": 12, "name": f"{prefix}collect HPC results", "conclusion": collect, "html_url": "https://job/12"},
    ]


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], log: Path) -> None:
        self.code = proc.returncode
        self.out = proc.stdout + proc.stderr
        raw = log.read_text() if log.exists() else ""
        self.calls = [block.splitlines() for block in raw.split("---\n") if block]
        self.reruns = [c for c in self.calls if any(a.endswith("/rerun") for a in c)]
        self.posts = [c for c in self.calls if any("/statuses/" in a for a in c)]

    def status(self) -> tuple[str, str, str]:
        (call,) = self.posts
        field = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in call if "=" in a and not a.startswith("/")}
        return field["state"], field["description"], field["target_url"]


def _run(
    tmp_path: Path,
    labels: tuple[str, ...],
    *,
    event: str = "pull_request_target",
    payload: dict[str, Any] | None = None,
    head_repo: str = REPO,
    head_sha: str = SHA,
    run: dict[str, Any] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    run_id: str = "5",
) -> Result:
    files = {
        "event": payload if payload is not None else {"pull_request": {"number": 42}},
        "pr": {
            "state": "open",
            "head": {"sha": head_sha, "repo": {"full_name": head_repo}},
            "labels": [{"name": n} for n in labels],
        },
        "run": run or {"status": "completed", "conclusion": "success", "run_attempt": 1, "html_url": "https://run/5"},
        "jobs": {"jobs": _jobs() if jobs is None else jobs},
    }
    for name, body in files.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(body))
    log = tmp_path / "gh.log"
    gh = (
        f'for a in "$@"; do printf "%s\\n" "$a"; done >> {log}\necho "token=$GH_TOKEN" >> {log}\necho --- >> {log}\n'
        'case "$*" in\n'
        "  *-X\\ POST*) ;;\n"
        f"  */workflows/*) echo {run_id} ;;\n"
        f"  */jobs*) cat {tmp_path}/jobs.json ;;\n"
        f"  */actions/runs/*) cat {tmp_path}/run.json ;;\n"
        f"  */pulls/*) cat {tmp_path}/pr.json ;;\n"
        '  *) echo unexpected gh call: "$*" >&2; exit 2 ;;\n'
        "esac"
    )
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}",
        "GH_TOKEN": "github-token",
        "RERUN_TOKEN": "app-token",
        "LABEL_INPUT": LABEL,
        "OPT_OUT_INPUT": OPT_OUT,
        "CONTEXT_INPUT": CONTEXT,
        "CI_WORKFLOW_INPUT": "ci.yml",
        "FORK_CI_WORKFLOW_INPUT": "ci-fork.yml",
        "RUN_URL": "https://example.invalid/run",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
        "GITHUB_REPOSITORY": REPO,
        "RUNNER_TEMP": str(tmp_path),
    }
    proc = subprocess.run(["bash", "-c", _body()], env=env, capture_output=True, text=True, check=False)
    return Result(proc, log)


def test_labelling_a_finished_run_re_runs_its_gate_with_the_app_token(tmp_path: Path) -> None:
    """The App's re-run, unlike github.token's, starts the workflows that follow CI."""
    r = _run(tmp_path, (LABEL,))
    assert r.code == 0, r.out
    (rerun,) = r.reruns
    assert f"repos/{REPO}/actions/jobs/11/rerun" in rerun
    assert "token=app-token" in rerun
    assert r.status() == ("pending", "HPC legs requested", "https://run/5")
    (post,) = r.posts
    assert "token=github-token" in post


def test_a_run_still_going_is_left_to_its_completion(tmp_path: Path) -> None:
    r = _run(tmp_path, (LABEL,), run={"status": "in_progress", "conclusion": None, "run_attempt": 1})
    assert (r.code, r.reruns, r.posts) == (0, [], []), r.out
    assert "still going" in r.out


@pytest.mark.parametrize(
    ("jobs", "attempt", "conclusion"),
    [(_jobs(gate="failure"), 1, "failure"), (_jobs(), 5, "success"), (_jobs(), 1, "cancelled")],
)
def test_no_re_run_loop_and_none_of_a_cancelled_run(
    tmp_path: Path, jobs: list[dict[str, Any]], attempt: int, conclusion: str
) -> None:
    run = {"status": "completed", "conclusion": conclusion, "run_attempt": attempt, "html_url": "https://run/5"}
    r = _run(tmp_path, (LABEL,), run=run, jobs=jobs)
    assert (r.code, r.reruns, r.posts) == (0, [], []), r.out


@pytest.mark.parametrize("labels", [(OPT_OUT,), (OPT_OUT, "bug")])
def test_the_opt_out_label_alone_passes(tmp_path: Path, labels: tuple[str, ...]) -> None:
    r = _run(tmp_path, labels, jobs=_jobs(collect="failure"))
    assert r.code == 0, r.out
    assert r.status() == ("success", "HPC CI not needed", "https://example.invalid/run")


@pytest.mark.parametrize("labels", [(), (LABEL,), (LABEL, OPT_OUT)])
@pytest.mark.parametrize("collected", ["success", "failure"])
def test_the_result_of_legs_that_ran_stands(tmp_path: Path, labels: tuple[str, ...], collected: str) -> None:
    r = _run(tmp_path, labels, jobs=_jobs(collect=collected))
    assert (r.code, r.reruns) == (0, []), r.out
    assert r.status() == (collected, f"HPC legs: {collected}", "https://job/12")


def test_neither_label_posts_nothing(tmp_path: Path) -> None:
    """Like the downstream statuses: the required check blocks as "Expected"."""
    r = _run(tmp_path, ("bug",))
    assert (r.code, r.reruns, r.posts) == (0, [], []), r.out


@pytest.mark.parametrize(("jobs", "run_id"), [([], "5"), (None, "")])
def test_a_ci_without_hpc_gate_or_no_ci_run_is_left_alone(
    tmp_path: Path, jobs: list[dict[str, Any]] | None, run_id: str
) -> None:
    r = _run(tmp_path, (OPT_OUT,), jobs=jobs, run_id=run_id)
    assert (r.code, r.reruns, r.posts) == (0, [], []), r.out


def _workflow_run(**fields: Any) -> dict[str, Any]:
    return {"workflow_run": {"id": 5, "path": ".github/workflows/ci.yml", "head_sha": SHA} | fields}


def test_a_finished_ci_run_re_runs_its_skipped_hpc_legs(tmp_path: Path) -> None:
    """The label came while CI ran, after hpc-gate had decided."""
    r = _run(tmp_path, (LABEL,), event="workflow_run", payload=_workflow_run(pull_requests=[{"number": 42}]))
    assert r.code == 0, r.out
    (rerun,) = r.reruns
    assert f"repos/{REPO}/actions/jobs/11/rerun" in rerun
    assert not any("/workflows/" in a for c in r.calls for a in c), "the event names the run"


@pytest.mark.parametrize(
    ("payload", "head_sha"),
    [(_workflow_run(pull_requests=[]), SHA), (_workflow_run(pull_requests=[{"number": 42}]), "b" * 40)],
)
def test_a_push_or_an_outdated_commit_is_left_alone(tmp_path: Path, payload: dict[str, Any], head_sha: str) -> None:
    r = _run(tmp_path, (LABEL,), event="workflow_run", payload=payload, head_sha=head_sha)
    assert (r.code, r.reruns, r.posts) == (0, [], []), r.out


def test_a_fork_ci_run_names_its_pull_request(tmp_path: Path) -> None:
    payload = _workflow_run(
        path=".github/workflows/ci-fork.yml", head_sha="b" * 40, display_title=f"CI (fork) #42 @ {SHA}"
    )
    r = _run(tmp_path, (LABEL,), event="workflow_run", payload=payload, head_repo=FORK, jobs=_jobs(prefix="ci / "))
    assert r.code == 0, r.out
    assert any(f"repos/{REPO}/pulls/42" in c for c in r.calls)
    (rerun,) = r.reruns
    assert f"repos/{REPO}/actions/jobs/11/rerun" in rerun


def test_a_fork_pull_request_finds_its_approved_fork_run(tmp_path: Path) -> None:
    r = _run(tmp_path, (LABEL,), head_repo=FORK)
    assert r.code == 0, r.out
    (lookup,) = (c for c in r.calls if any("/workflows/" in a for a in c))
    assert any("workflows/ci-fork.yml/runs" in a for a in lookup)
    assert any(f'endswith("#42 @ {SHA}")' in a for a in lookup)
