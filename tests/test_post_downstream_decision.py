# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""post-downstream-decision's `run:` body, executed with a stub `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import action_run_body, stub_gh

LABEL: Final = "run-downstream-ci"
OPT_OUT: Final = "downstream-ci-not-needed"
CONTEXTS: Final = ("downstream/runner", "downstream/hpc-atos")


def _payload(action: str = "opened", *, labels: list[str] | None = None, label: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {"number": 42, "head": {"sha": "abc123"}, "labels": [{"name": n} for n in labels or []]},
    }
    if label is not None:
        payload["label"] = {"name": label}
    return payload


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], gh_log: Path, outputs: Path) -> None:
        self.code = proc.returncode
        self.err = proc.stderr
        raw = gh_log.read_text() if gh_log.exists() else ""
        self.gh_calls = [block.splitlines() for block in raw.split("---\n") if block]
        self.outputs = outputs.read_text() if outputs.exists() else ""

    @property
    def decision(self) -> str | None:
        for line in self.outputs.splitlines():
            if line.startswith("decision="):
                return line.removeprefix("decision=")
        return None

    def posted(self, field: str) -> list[str]:
        """The `-f field=...` value of each status POST."""
        prefix = f"{field}="
        return [next(a.removeprefix(prefix) for a in call if a.startswith(prefix)) for call in self.gh_calls]


def _run(tmp_path: Path, payload: dict[str, Any]) -> Result:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    gh_log = tmp_path / "gh-calls.log"
    bindir = stub_gh(tmp_path, f'for a in "$@"; do printf "%s\\n" "$a"; done >> {gh_log}\necho --- >> {gh_log}')
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_REPOSITORY": "ecmwf/eckit",
        "RUN_URL": "https://example.invalid/run",
        "CONTEXTS_INPUT": " ".join(CONTEXTS),
        "LABEL_INPUT": LABEL,
        "OPT_OUT_INPUT": OPT_OUT,
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("post-downstream-decision")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return Result(proc, gh_log, outputs)


@pytest.mark.parametrize("action", ["opened", "synchronize", "unlabeled"])
def test_undecided_posts_nothing(tmp_path: Path, action: str) -> None:
    """The required statuses stay "Expected", which blocks the merge without anybody posting."""
    r = _run(tmp_path, _payload(action))
    assert r.code == 0
    assert r.decision == "undecided"
    assert r.gh_calls == []


@pytest.mark.parametrize("level", ["all", "2"])
def test_setting_a_level_label_makes_every_lane_pending(tmp_path: Path, level: str) -> None:
    r = _run(tmp_path, _payload("labeled", labels=[f"{LABEL}:{level}"], label=f"{LABEL}:{level}"))
    assert r.code == 0
    assert r.decision == "run"
    assert r.posted("context") == list(CONTEXTS)
    assert r.posted("state") == ["pending", "pending"]
    assert r.gh_calls[0][3] == "/repos/ecmwf/eckit/statuses/abc123"


@pytest.mark.parametrize(
    ("action", "label"), [("synchronize", None), ("labeled", "documentation"), ("labeled", OPT_OUT)]
)
def test_a_run_decision_leaves_the_lanes_result(tmp_path: Path, action: str, label: str | None) -> None:
    """A new commit has no statuses yet; on the same commit, the lanes' result stands."""
    r = _run(tmp_path, _payload(action, labels=[f"{LABEL}:1", OPT_OUT], label=label))
    assert r.code == 0
    assert r.decision == "run"
    assert r.gh_calls == []


@pytest.mark.parametrize("action", ["labeled", "synchronize"])
def test_the_opt_out_label_passes_every_lane(tmp_path: Path, action: str) -> None:
    r = _run(tmp_path, _payload(action, labels=[OPT_OUT], label=OPT_OUT))
    assert r.code == 0
    assert r.decision == "skip"
    assert r.posted("context") == list(CONTEXTS)
    assert r.posted("state") == ["success", "success"]


@pytest.mark.parametrize("labels", [[f"{LABEL}:1", f"{LABEL}:all"], [LABEL], [f"{LABEL}:0"], [f"{LABEL}:x"]])
def test_an_invalid_level_fails_every_lane(tmp_path: Path, labels: list[str]) -> None:
    """Fails rather than staying silent: an earlier success on this commit must not stand."""
    r = _run(tmp_path, _payload("labeled", labels=labels, label=labels[-1]))
    assert r.code == 0
    assert r.decision == "conflict"
    assert r.posted("state") == ["failure", "failure"]


def test_no_pull_request_fails(tmp_path: Path) -> None:
    r = _run(tmp_path, {"action": "labeled"})
    assert r.code == 1
    assert "only runs on pull_request" in r.err
