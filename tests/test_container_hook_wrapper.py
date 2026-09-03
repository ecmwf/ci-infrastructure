# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for runners/container-hook-wrapper.js.

This file sits in the start path of every job on a scale set that enables it, so
the properties worth pinning are the pass-through ones: the real hook must
receive the payload byte-identically and its exit code must survive, whatever the
wrapper thinks of the input.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

WRAPPER: Final = Path(__file__).resolve().parent.parent / "runners" / "container-hook-wrapper.js"

# The runner invokes a .js hook with its own bundled node, so on a runner this is
# never absent. Locally it may be; skipping beats a red suite for a missing
# interpreter this repo does not otherwise need.
NODE: Final = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

#: Shaped after actions/runner-container-hooks examples/prepare-job.json.
PREPARE_JOB: Final[dict[str, Any]] = {
    "command": "prepare_job",
    "responseFile": "/w/_temp/response.json",
    "state": {},
    "args": {
        "container": {
            "image": "eccr.ecmwf.int/public-ci-images/rocky8-gfortran13-boost-qt5:latest",
            "workingDirectory": "/__w/stack-dependencies",
            "environmentVariables": {"NODE_ENV": "development"},
            "registry": {"username": "u", "password": "p", "serverUrl": "https://index.docker.io/v1"},
        },
        "services": [{"contextName": "redis", "image": "redis:7"}],
    },
}


def _run(tmp_path: Path, payload: str, exit_code: int = 0) -> tuple[subprocess.CompletedProcess[str], str]:
    """Drive the wrapper against a fake hook; return the result and what the fake received."""
    received = tmp_path / "received.json"
    fake = tmp_path / "fake-hook.js"
    fake.write_text(
        "const fs = require('fs');\n"
        "fs.writeFileSync(process.env.FAKE_RECEIVED, fs.readFileSync(0, 'utf8'));\n"
        f"process.exit({exit_code});\n"
    )
    assert NODE is not None  # guarded by pytestmark
    proc = subprocess.run(
        [NODE, str(WRAPPER)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "CI_REAL_CONTAINER_HOOK": str(fake), "FAKE_RECEIVED": str(received)},
    )
    return proc, received.read_text() if received.exists() else ""


def test_prepare_job_names_the_job_and_service_images(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, json.dumps(PREPARE_JOB))
    assert proc.returncode == 0
    assert "job container: eccr.ecmwf.int/public-ci-images/rocky8-gfortran13-boost-qt5:latest" in proc.stdout
    assert "service container: redis:7" in proc.stdout


def test_other_commands_print_nothing(tmp_path: Path) -> None:
    """Only prepare_job knows an image; the rest must not add noise to every step."""
    payload = json.dumps({"command": "run_script_step", "responseFile": None, "state": {}, "args": {}})
    proc, _ = _run(tmp_path, payload)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_payload_reaches_the_real_hook_byte_identically(tmp_path: Path) -> None:
    payload = json.dumps(PREPARE_JOB)
    _, received = _run(tmp_path, payload)
    assert received == payload


@pytest.mark.parametrize("code", [0, 7])
def test_the_real_hooks_exit_code_survives(tmp_path: Path, code: int) -> None:
    proc, _ = _run(tmp_path, json.dumps(PREPARE_JOB), exit_code=code)
    assert proc.returncode == code


def test_unparseable_payload_still_delegates(tmp_path: Path) -> None:
    """Logging must never be the reason a job fails to start."""
    proc, received = _run(tmp_path, "not json at all", exit_code=3)
    assert proc.returncode == 3
    assert received == "not json at all"
    assert proc.stdout == ""
