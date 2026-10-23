# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""ci-result's `run:` body against needs contexts, with a stub `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import action_run_body, stub_gh

REPO = "ecmwf/eckit"


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], gh_log: Path, summary: Path) -> None:
        self.code = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.summary = summary.read_text() if summary.exists() else ""
        raw = gh_log.read_text() if gh_log.exists() else ""
        self.posts = [block.splitlines() for block in raw.split("---\n") if block]

    def posted(self, field: str) -> str:
        (call,) = self.posts
        (value,) = (a.removeprefix(f"{field}=") for a in call if a.startswith(f"{field}="))
        return value


def _run(tmp_path: Path, needs: str, event: str = "push", payload: dict[str, Any] | None = None) -> Result:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(payload or {}))
    gh_log = tmp_path / "gh.log"
    bindir = stub_gh(tmp_path, f'for a in "$@"; do printf "%s\\n" "$a"; done >> {gh_log}\necho --- >> {gh_log}')
    summary = tmp_path / "summary.md"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CONTEXT_INPUT": "ci-result",
        "RUN_URL": "https://example.invalid/run",
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": "pushsha",
    }
    # A file, as the runner does: one argument holds at most 128 KiB.
    script = tmp_path / "step.sh"
    script.write_text(action_run_body("ci-result").replace("${{ inputs.needs }}", needs))
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    return Result(proc, gh_log, summary)


def _needs(**results: str) -> str:
    return json.dumps({job: {"result": r, "outputs": {}} for job, r in results.items()}, indent=2)


def _pr(head_repo: str) -> dict[str, Any]:
    return {"pull_request": {"head": {"sha": "headsha", "repo": {"full_name": head_repo}}}}


def test_all_success_posts_success(tmp_path: Path) -> None:
    r = _run(tmp_path, _needs(build="success", test="success"))
    assert r.code == 0, r.out
    assert "| test | success |" in r.summary
    assert f"/repos/{REPO}/statuses/pushsha" in r.posts[0]
    assert r.posted("state") == "success"
    assert r.posted("context") == "ci-result"


@pytest.mark.parametrize("bad", ["failure", "cancelled", "skipped"])
def test_any_other_result_posts_failure(tmp_path: Path, bad: str) -> None:
    r = _run(tmp_path, _needs(build="success", test=bad))
    assert r.code == 1
    assert f"::error title=CI result::test ({bad})" in r.out
    assert r.posted("state") == "failure"
    assert r.posted("description") == "Not green: test"


def test_needs_beyond_the_env_var_limit(tmp_path: Path) -> None:
    """Outputs can pass the 128 KiB one env var may hold, and may hold shell syntax."""
    needs = {"setup": {"result": "success", "outputs": {"m": "x" * 300_000, "q": "'\"$(false)` CI_RESULT_NEEDS_EOF"}}}
    r = _run(tmp_path, json.dumps(needs, indent=2))
    assert r.code == 0, r.out
    assert r.posted("description") == "All 1 jobs succeeded"


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_pull_request_posts_onto_head(tmp_path: Path, event: str) -> None:
    r = _run(tmp_path, _needs(build="success"), event, _pr(REPO if event == "pull_request" else "someone/eckit"))
    assert r.code == 0, r.out
    assert f"/repos/{REPO}/statuses/headsha" in r.posts[0]


def test_forks_own_pull_request_run_posts_nothing(tmp_path: Path) -> None:
    r = _run(tmp_path, _needs(**{"ci-approval": "skipped"}), "pull_request", _pr("someone/eckit"))
    assert r.code == 0, r.out
    assert r.posts == []
    assert "ci-fork.yml posts ci-result" in r.out


@pytest.mark.parametrize("needs", ["", "{}", "[]", "not json"])
def test_missing_needs_posts_failure(tmp_path: Path, needs: str) -> None:
    r = _run(tmp_path, needs)
    assert r.code == 1
    assert "toJSON(needs)" in r.out
    assert r.posted("state") == "failure"
