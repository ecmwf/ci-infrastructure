# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""dispatch-downstream's `run:` body, executed against synthetic payloads with a stub `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import action_run_body, stub_gh


def _dispatch(tmp_path: Path, action: str, label: str | None) -> tuple[int, list[str], str]:
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {"number": 7},
        "repository": {"default_branch": "develop"},
        **({"label": {"name": label}} if label else {}),
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    calls = tmp_path / "calls"
    gh = 'printf "%s\\n" "$*" >> ' + str(calls)
    env = {
        **os.environ,
        "PATH": f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "ecmwf/eckit",
        "WORKFLOWS_INPUT": "trigger-downstream.yml trigger-downstream-hpc-atos.yml",
        "LABEL_INPUT": "run-downstream-ci",
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("dispatch-downstream")], env=env, capture_output=True, text=True, check=False
    )
    return proc.returncode, calls.read_text().splitlines() if calls.exists() else [], proc.stdout + proc.stderr


@pytest.mark.parametrize("level", ["1", "all"])
def test_a_level_label_dispatches_every_lane(tmp_path: Path, level: str) -> None:
    code, calls, _ = _dispatch(tmp_path, "labeled", f"run-downstream-ci:{level}")
    assert code == 0
    assert calls == [
        f"api -X POST repos/ecmwf/eckit/actions/workflows/{wf}/dispatches "
        f"-f ref=develop -f inputs[pull-request]=7 -f inputs[level]={level}"
        for wf in ("trigger-downstream.yml", "trigger-downstream-hpc-atos.yml")
    ]


@pytest.mark.parametrize(
    ("action", "label"),
    [
        ("labeled", "downstream-ci-not-needed"),
        ("labeled", "run-downstream-ci"),
        ("labeled", "run-downstream-ci:0"),
        ("labeled", "run-downstream-ci:x"),
        ("unlabeled", "run-downstream-ci:1"),
        ("synchronize", None),
    ],
    ids=["other-label", "bare", "zero", "word", "unlabeled", "push"],
)
def test_anything_else_dispatches_nothing(tmp_path: Path, action: str, label: str | None) -> None:
    code, calls, log = _dispatch(tmp_path, action, label)
    assert (code, calls) == (0, [])
    assert "nothing to dispatch" in log
