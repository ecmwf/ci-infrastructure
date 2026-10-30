# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""hpc-gate's `run:` body, with a stub `gh` that answers the labels lookup and logs status posts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import action_run_body, stub_gh

REPO: Final = "ecmwf/eckit"
LABEL: Final = "run-hpc-ci"
OPT_OUT: Final = "hpc-ci-not-needed"
CONTEXT: Final = "ci-result/hpc-atos"


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], gh_log: Path, outputs: Path) -> None:
        self.code = proc.returncode
        self.out = proc.stdout + proc.stderr
        raw = gh_log.read_text() if gh_log.exists() else ""
        calls = [block.splitlines() for block in raw.split("---\n") if block]
        self.lookups = [c for c in calls if "-X" not in c]
        self.posts = [c for c in calls if "-X" in c]
        lines = outputs.read_text().splitlines() if outputs.exists() else []
        self.run = next((ln.removeprefix("run=") for ln in lines if ln.startswith("run=")), None)

    def posted(self, field: str) -> str:
        (call,) = self.posts
        (value,) = (a.removeprefix(f"{field}=") for a in call if a.startswith(f"{field}="))
        return value


def _pr(head_repo: str = REPO) -> dict[str, Any]:
    """Labelled in the payload, which is stale on a re-run: the gate must read the live labels."""
    return {
        "pull_request": {
            "number": 42,
            "head": {"sha": "headsha", "repo": {"full_name": head_repo}},
            "labels": [{"name": LABEL}],
        }
    }


def _run(
    tmp_path: Path,
    payload: dict[str, Any],
    live: tuple[str, ...] = (),
    event: str = "pull_request",
    gh_fails: bool = False,
) -> Result:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(payload))
    labels = tmp_path / "labels.txt"
    labels.write_text("".join(f"{n}\n" for n in live))
    gh_log = tmp_path / "gh.log"
    gh = (
        f'for a in "$@"; do printf "%s\\n" "$a"; done >> {gh_log}\necho --- >> {gh_log}\n'
        + ("exit 1\n" if gh_fails else "")
        + f'case "$*" in *"/labels"*) cat {labels} ;; esac'
    )
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}",
        "LABEL_INPUT": LABEL,
        "OPT_OUT_INPUT": OPT_OUT,
        "CONTEXT_INPUT": CONTEXT,
        "RUN_URL": "https://example.invalid/run",
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": "pushsha",
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("hpc-gate")], env=env, capture_output=True, text=True, check=False
    )
    return Result(proc, gh_log, outputs)


def test_non_pull_request_runs_without_a_lookup(tmp_path: Path) -> None:
    r = _run(tmp_path, {}, event="push")
    assert (r.code, r.run, r.lookups, r.posts) == (0, "true", [], [])


@pytest.mark.parametrize(
    ("live", "run", "state", "description"),
    [
        (("bug", LABEL), "true", "pending", "HPC legs running"),
        ((LABEL, OPT_OUT), "true", "pending", "HPC legs running"),
        (("bug", OPT_OUT), "false", "success", "HPC CI not needed"),
    ],
)
@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_the_labels_decide_the_run_and_the_status(
    tmp_path: Path, event: str, live: tuple[str, ...], run: str, state: str, description: str
) -> None:
    r = _run(tmp_path, _pr(), live, event)
    assert (r.code, r.run) == (0, run), r.out
    (lookup,) = r.lookups
    assert f"repos/{REPO}/issues/42/labels" in lookup
    (post,) = r.posts
    assert f"/repos/{REPO}/statuses/headsha" in post
    assert (r.posted("state"), r.posted("description")) == (state, description)
    assert r.posted("context") == CONTEXT
    assert r.posted("target_url") == "https://example.invalid/run"


def test_forks_own_pull_request_run_posts_nothing(tmp_path: Path) -> None:
    r = _run(tmp_path, _pr("someone/eckit"), (LABEL,))
    assert (r.code, r.run, r.posts) == (0, "true", []), r.out
    assert f"ci-fork.yml posts {CONTEXT}" in r.out


def test_fork_under_pull_request_target_posts(tmp_path: Path) -> None:
    r = _run(tmp_path, _pr("someone/eckit"), (OPT_OUT,), event="pull_request_target")
    assert (r.code, r.run) == (0, "false"), r.out
    assert r.posted("state") == "success"


def test_failed_label_lookup_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, _pr(), (LABEL,), gh_fails=True)
    assert (r.code, r.run, r.posts) == (1, None, [])
    assert "could not read the labels of #42" in r.out


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_neither_label_skips_and_posts_nothing(tmp_path: Path, event: str) -> None:
    """Like the downstream statuses: the required check blocks as "Expected"."""
    r = _run(tmp_path, _pr(), ("bug", f"{LABEL}-not"), event)
    assert (r.code, r.run, r.posts) == (0, "false", []), r.out
