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

BASE: Final = "ecmwf/eckit"
FORK: Final = "outsider/eckit"
HEAD: Final = "a" * 40
PERMISSIONS: Final = {"maintainer": "write", "lead": "admin", "helper": "triage", "outsider": "read"}


def _payload(head_repo: str | None) -> dict[str, Any]:
    return {
        "action": "synchronize",
        "pull_request": {
            "number": 42,
            "head": {"sha": HEAD, "repo": None if head_repo is None else {"full_name": head_repo}},
            "base": {"repo": {"full_name": BASE}},
        },
    }


def _run(
    tmp_path: Path, payload: dict[str, Any] | None, *, actor: str = "outsider", inputs: dict[str, str] | None = None
) -> tuple[int, str | None, str, list[str]]:
    """`payload=None` is a non-pull-request event; a user missing from PERMISSIONS 404s."""
    tmp_path.mkdir(exist_ok=True)
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload if payload is not None else {"action": "completed"}))
    for user, permission in PERMISSIONS.items():
        (tmp_path / f"perm-{user}").write_text(permission)
    log = tmp_path / "gh-calls.log"
    gh = (
        f'printf "%s\\n" "$*" >> {log}\n'
        'user="${2#*/collaborators/}"; user="${user%/permission}"\n'
        f'[ -f {tmp_path}/perm-"$user" ] || exit 1; cat {tmp_path}/perm-"$user"'
    )
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_REPOSITORY": BASE,
        "GITHUB_TRIGGERING_ACTOR": actor,
        "TRUST_SAME_REPO_INPUT": "",
        **(inputs or {}),
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("require-ci-approval")], env=env, capture_output=True, text=True, check=False
    )
    reasons = [ln.removeprefix("reason=") for ln in outputs.read_text().splitlines()] if outputs.exists() else []
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, (reasons[0] if reasons else None), proc.stderr, calls


def test_a_non_pull_request_event_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, None)[:2] == (0, "not-a-pull-request")


def test_a_branch_in_the_base_repo_passes_without_a_lookup(tmp_path: Path) -> None:
    code, reason, _, calls = _run(tmp_path, _payload(BASE))
    assert (code, reason, calls) == (0, "same-repo", [])


def test_trust_same_repo_false_demands_a_maintainers_run_from_everyone(tmp_path: Path) -> None:
    strict = {"TRUST_SAME_REPO_INPUT": "false"}
    assert _run(tmp_path / "outsider", _payload(BASE), inputs=strict)[0] == 1
    assert _run(tmp_path / "maintainer", _payload(BASE), actor="maintainer", inputs=strict)[0] == 0


@pytest.mark.parametrize("head_repo", [FORK, None], ids=["fork", "deleted-fork"])
def test_the_forks_own_run_fails_and_says_how_to_approve(tmp_path: Path, head_repo: str | None) -> None:
    code, reason, err, _ = _run(tmp_path, _payload(head_repo))
    assert (code, reason) == (1, None)
    assert f"a maintainer must read commit {HEAD[:12]} and re-run this run" in err
    assert "Re-run all jobs" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("actor", ["maintainer", "lead"])
def test_a_re_run_by_someone_with_write_access_approves(tmp_path: Path, actor: str) -> None:
    code, reason, _, calls = _run(tmp_path, _payload(FORK), actor=actor)
    assert (code, reason) == (0, "approved")
    assert calls == [f"api repos/{BASE}/collaborators/{actor}/permission --jq .permission"]


def test_a_re_run_by_someone_without_write_access_does_not(tmp_path: Path) -> None:
    assert _run(tmp_path, _payload(FORK), actor="helper")[0] == 1


def test_a_failed_permission_lookup_says_so(tmp_path: Path) -> None:
    code, _, err, _ = _run(tmp_path, _payload(FORK), actor="stranger")
    assert code == 1
    assert "could not look up the permission of @stranger" in err


@pytest.mark.parametrize("value", ["yes", "True ", "1"])
def test_a_non_boolean_input_stops_the_workflow(tmp_path: Path, value: str) -> None:
    code, _, err, _ = _run(tmp_path, _payload(FORK), inputs={"TRUST_SAME_REPO_INPUT": value})
    assert code == 1
    assert "must be 'true' or 'false'" in err
