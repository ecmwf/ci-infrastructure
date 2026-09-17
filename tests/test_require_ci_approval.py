# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""require-ci-approval's `run:` body, executed against synthetic payloads with a stub `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import action_run_body, stub_gh

LABEL: Final = "approved-for-ci"
BASE: Final = "ecmwf/eckit"
FORK: Final = "outsider/eckit"


def _payload(*, action: str, head_repo: str | None, labels: list[str]) -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {
            "number": 42,
            "head": {"repo": None if head_repo is None else {"full_name": head_repo}},
            "base": {"repo": {"full_name": BASE}},
            "labels": [{"name": n} for n in labels],
        },
    }


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], gh_log: Path, outputs: Path) -> None:
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr
        self.gh_calls = gh_log.read_text().splitlines() if gh_log.exists() else []
        self.outputs = outputs.read_text() if outputs.exists() else ""

    @property
    def reason(self) -> str | None:
        for line in self.outputs.splitlines():
            if line.startswith("reason="):
                return line.removeprefix("reason=")
        return None


def _run(
    tmp_path: Path,
    payload: dict[str, Any] | None,
    *,
    gh_fails: bool = False,
    inputs: dict[str, str] | None = None,
) -> Result:
    """`payload=None` is a non-pull-request event (workflow_run, schedule)."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload if payload is not None else {"action": "completed"}))

    gh_log = tmp_path / "gh-calls.log"
    bindir = stub_gh(tmp_path, f'printf "%s\\n" "$*" >> {gh_log}\nexit {1 if gh_fails else 0}')

    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_REPOSITORY": BASE,
        "LABEL_INPUT": "",
        "CONSUME_INPUT": "",
        "TRUST_SAME_REPO_INPUT": "",
        **(inputs or {}),
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("require-ci-approval")], env=env, capture_output=True, text=True, check=False
    )
    return Result(proc, gh_log, outputs)


def _deletes(result: Result) -> list[str]:
    return [c for c in result.gh_calls if c.startswith("api -X DELETE")]


# === The passing paths ======================================================


def test_a_non_pull_request_event_passes_untouched(tmp_path: Path) -> None:
    r = _run(tmp_path, None)

    assert r.code == 0
    assert r.reason == "not-a-pull-request"
    assert _deletes(r) == []


def test_a_branch_in_the_base_repo_passes_untouched(tmp_path: Path) -> None:
    """Pushing there already needed write access, so no label is spent."""
    r = _run(tmp_path, _payload(action="opened", head_repo=BASE, labels=[LABEL]))

    assert r.code == 0
    assert r.reason == "same-repo"
    assert _deletes(r) == []


def test_trust_same_repo_false_demands_the_label_from_everyone(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        _payload(action="opened", head_repo=BASE, labels=[]),
        inputs={"TRUST_SAME_REPO_INPUT": "false"},
    )

    assert r.code == 1


# === The label is spent when it is honoured =================================


def test_an_approved_fork_passes_and_the_label_is_deleted(tmp_path: Path) -> None:
    """One approval buys one run."""
    r = _run(tmp_path, _payload(action="labeled", head_repo=FORK, labels=[LABEL, "bug"]))

    assert r.code == 0
    assert r.reason == "approved"
    assert _deletes(r) == [f"api -X DELETE repos/{BASE}/issues/42/labels/{LABEL}"]


def test_the_label_name_is_url_encoded(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        _payload(action="labeled", head_repo=FORK, labels=["ready for CI/HPC"]),
        inputs={"LABEL_INPUT": "ready for CI/HPC"},
    )

    assert r.code == 0
    assert _deletes(r) == [f"api -X DELETE repos/{BASE}/issues/42/labels/ready%20for%20CI%2FHPC"]


def test_a_failed_delete_fails_the_job(tmp_path: Path) -> None:
    r = _run(tmp_path, _payload(action="labeled", head_repo=FORK, labels=[LABEL]), gh_fails=True)

    assert r.code == 1
    assert "could not remove the approval label" in r.err
    assert r.reason is None


def test_consume_label_false_leaves_the_label_alone(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        _payload(action="labeled", head_repo=FORK, labels=[LABEL]),
        inputs={"CONSUME_INPUT": "false"},
    )

    assert r.code == 0
    assert r.reason == "approved"
    assert _deletes(r) == []


# === The failing paths ======================================================


def test_an_unapproved_fork_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, _payload(action="opened", head_repo=FORK, labels=["bug"]))

    assert r.code == 1
    assert r.reason is None


def test_a_deleted_fork_counts_as_a_fork(tmp_path: Path) -> None:
    """head.repo is null once the fork is gone; fail closed."""
    r = _run(tmp_path, _payload(action="opened", head_repo=None, labels=[]))

    assert r.code == 1


@pytest.mark.parametrize("action", ["synchronize", "reopened"])
def test_a_push_fails_and_sweeps_up_a_leftover_label(tmp_path: Path, action: str) -> None:
    """A leftover label means the run that should have spent it was cancelled."""
    r = _run(tmp_path, _payload(action=action, head_repo=FORK, labels=[LABEL]))

    assert r.code == 1
    assert _deletes(r) == [f"api -X DELETE repos/{BASE}/issues/42/labels/{LABEL}"]


@pytest.mark.parametrize("action", ["synchronize", "reopened"])
def test_a_push_fails_from_the_payload_alone(tmp_path: Path, action: str) -> None:
    r = _run(tmp_path, _payload(action=action, head_repo=FORK, labels=[]), gh_fails=True)

    assert r.code == 1
    assert _deletes(r) == []


# === Input validation =======================================================


@pytest.mark.parametrize("value", ["yes", "True ", "1"])
def test_a_non_boolean_input_stops_the_workflow(tmp_path: Path, value: str) -> None:
    r = _run(
        tmp_path,
        _payload(action="labeled", head_repo=FORK, labels=[LABEL]),
        inputs={"CONSUME_INPUT": value},
    )

    assert r.code == 1
    assert "must be 'true' or 'false'" in r.err
    assert _deletes(r) == []
