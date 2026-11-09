# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""require-ci-approval's `run:` body, executed against synthetic payloads with a stub `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from conftest import REPO_ROOT, action_run_body, stub_gh

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
        ["bash", "-c", action_run_body("require-ci-approval", "gate")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
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


def test_a_non_boolean_check_workflows_stops_the_workflow(tmp_path: Path) -> None:
    code, _, err, _ = _run(tmp_path, None, inputs={"CHECK_WORKFLOWS_INPUT": "yes"})
    assert code == 1
    assert "input 'check-workflows' must be 'true' or 'false'" in err


# --- check-workflows: the checks over the checked-out workflows -------------------------------

CHECKOUT: Final = ".require-ci-approval-checkout"
CI_YML: Final = """
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps: [{run: make}]
  ci-result:
    name: collect results
    if: always()
    needs: [build]
    runs-on: ubuntu-latest
    permissions:
      statuses: write
    steps:
      - uses: ecmwf/ci-infrastructure/actions/ci-result@main
        with:
          needs: ${{ toJSON(needs) }}
"""
UNGATED_PRT: Final = """
on: pull_request_target
jobs:
  build:
    runs-on: ubuntu-latest
    steps: [{run: make}]
"""


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load((REPO_ROOT / "actions" / "require-ci-approval" / "action.yml").read_text(encoding="utf-8"))
    return list(doc["runs"]["steps"])


def _check(tmp_path: Path, files: dict[str, str]) -> tuple[int, str]:
    """Run the check step over a checkout holding `files`; returns (exit code, stdout+stderr)."""
    for rel, body in files.items():
        (tmp_path / CHECKOUT / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / CHECKOUT / rel).write_text(body)
    env = {**os.environ, "GITHUB_WORKSPACE": str(tmp_path), "CHECKOUT_DIR": CHECKOUT}
    env["CI_INFRASTRUCTURE_PYTHON"] = sys.executable
    proc = subprocess.run(
        ["bash", "-c", action_run_body("require-ci-approval", "check")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_well_formed_workflows_pass_and_the_checkout_is_removed(tmp_path: Path) -> None:
    code, out = _check(tmp_path, {".github/workflows/ci.yml": CI_YML})
    assert code == 0, out
    assert "1 workflow(s) pass" in out
    assert not (tmp_path / CHECKOUT).exists()


def test_a_ci_yml_without_ci_result_fails_as_a_workflow_check(tmp_path: Path) -> None:
    code, out = _check(tmp_path, {".github/workflows/ci.yml": "on: push\njobs:\n  build:\n    runs-on: x\n"})
    assert code == 1
    assert "::error title=Workflow check::.github/workflows/ci.yml:" in out
    assert "title=CI approval" not in out


def test_an_ungated_pull_request_target_workflow_fails(tmp_path: Path) -> None:
    code, out = _check(tmp_path, {".github/workflows/ci.yml": CI_YML, ".github/workflows/x.yml": UNGATED_PRT})
    assert code == 1
    assert "::error title=Workflow check::.github/workflows/x.yml:" in out


def test_the_allowlist_and_manifest_of_the_checkout_count(tmp_path: Path) -> None:
    allowlist = {".github/ci-approval-allowlist.yml": "exempt:\n  - workflow: x.yml\n    reason: test\n"}
    private = {".ci/manifest.toml": '[package]\nvisibility = "private"\n'}
    base = {".github/workflows/ci.yml": CI_YML, ".github/workflows/x.yml": UNGATED_PRT}
    assert _check(tmp_path / "allowlist", {**base, **allowlist})[0] == 0
    assert _check(tmp_path / "private", {**base, **private})[0] == 0


def test_nothing_in_the_checkout_is_imported(tmp_path: Path) -> None:
    planted = "raise SystemExit('planted module imported')\n"
    files = {".github/workflows/ci.yml": CI_YML, "yaml.py": planted, "ci_infrastructure/__init__.py": planted}
    code, out = _check(tmp_path, files)
    assert code == 0, out


def test_the_checkout_follows_the_gate_and_checks_out_only_what_the_checks_read() -> None:
    steps = _steps()
    ids = [s.get("id") for s in steps]
    assert ids.index("gate") < ids.index("check")
    (checkout,) = (s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert ids.index("gate") < steps.index(checkout) < ids.index("check")
    assert checkout["if"] == "inputs.check-workflows == 'true'"
    assert checkout["with"]["path"] == CHECKOUT
    assert checkout["with"]["sparse-checkout"].split() == [".github", ".ci"]
    assert checkout["with"]["persist-credentials"] is False


def test_it_checks_the_commit_checkout_under_test_checks_out() -> None:
    (checkout,) = (s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@"))
    doc = yaml.safe_load((REPO_ROOT / "actions" / "checkout-under-test" / "action.yml").read_text(encoding="utf-8"))
    (under_test,) = doc["runs"]["steps"]
    assert under_test["uses"] == checkout["uses"]
    assert under_test["with"]["ref"].replace("inputs.ref || ", "") == checkout["with"]["ref"]
