# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""hpc-gate's `run:` body, with a stub `gh` that answers the labels lookup and logs every call."""

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


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], gh_log: Path, outputs: Path) -> None:
        self.code = proc.returncode
        self.out = proc.stdout + proc.stderr
        raw = gh_log.read_text() if gh_log.exists() else ""
        self.calls = [block.splitlines() for block in raw.split("---\n") if block]
        lines = outputs.read_text().splitlines() if outputs.exists() else []
        self.run = next((ln.removeprefix("run=") for ln in lines if ln.startswith("run=")), None)


def _pr() -> dict[str, Any]:
    """Labelled in the payload, which is stale on a re-run: the gate must read the live labels."""
    return {"pull_request": {"number": 42, "labels": [{"name": LABEL}]}}


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
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": REPO,
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("hpc-gate")], env=env, capture_output=True, text=True, check=False
    )
    return Result(proc, gh_log, outputs)


def test_non_pull_request_runs_without_a_lookup(tmp_path: Path) -> None:
    r = _run(tmp_path, {}, event="push")
    assert (r.code, r.run, r.calls) == (0, "true", [])


@pytest.mark.parametrize(
    ("live", "run"),
    [
        (("bug", LABEL), "true"),
        ((LABEL, "hpc-ci-not-needed"), "true"),
        (("bug", f"{LABEL}-not"), "false"),
        ((), "false"),
    ],
)
@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_the_live_label_decides_and_nothing_is_posted(
    tmp_path: Path, event: str, live: tuple[str, ...], run: str
) -> None:
    r = _run(tmp_path, _pr(), live, event)
    assert (r.code, r.run) == (0, run), r.out
    (lookup,) = r.calls
    assert f"repos/{REPO}/issues/42/labels" in lookup
    assert "-X" not in lookup


def test_failed_label_lookup_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, _pr(), (LABEL,), gh_fails=True)
    assert (r.code, r.run) == (1, None)
    assert "could not read the labels of #42" in r.out
