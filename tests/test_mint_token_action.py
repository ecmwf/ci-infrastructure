# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""mint-token's check step: the inputs handed to create-github-app-token, whichever App the key is."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import action_run_body


def _check(tmp_path: Path, repositories: str, permissions: str) -> tuple[int, str, dict[str, str]]:
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "OWNER": "ecmwf",
        "REPOSITORIES": repositories,
        "PERMISSIONS": permissions,
        "GITHUB_OUTPUT": str(outputs),
    }
    proc = subprocess.run(
        ["bash", "-c", action_run_body("mint-token", "check")], env=env, capture_output=True, text=True, check=False
    )
    lines = outputs.read_text().splitlines() if outputs.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, dict(ln.split("=", 1) for ln in lines)


def test_a_token_names_its_repositories_and_permissions(tmp_path: Path) -> None:
    code, out, got = _check(tmp_path, "eckit, ecmwf/eccodes\nfdb", "contents:read,actions:write")
    assert code == 0, out
    assert got == {
        "actions": "write",
        "administration": "",
        "checks": "",
        "contents": "read",
        "pull-requests": "",
        "repositories": "eckit,eccodes,fdb",
    }


@pytest.mark.parametrize("repositories", ["*", " *\n"])
def test_a_star_takes_every_installed_repository(tmp_path: Path, repositories: str) -> None:
    code, out, got = _check(tmp_path, repositories, "pull-requests:write")
    assert code == 0, out
    assert got["repositories"] == ""
    assert got["pull-requests"] == "write"


@pytest.mark.parametrize(
    ("repositories", "permissions", "message"),
    [
        ("eckit", "statuses:read", "'statuses' is not one of"),
        ("eckit", "contents:admin", "is not name:read or name:write"),
        ("eckit", "contents", "is not name:read or name:write"),
        ("eckit", "", "permissions is empty"),
        ("", "contents:read", "repositories is empty"),
        ("other/eckit", "contents:read", "not a repository of ecmwf"),
        ("eck$it", "contents:read", "not a repository name"),
        ("* eckit", "pull-requests:write", "'*' is not a repository name"),
        (",*,", "pull-requests:write", "'*' is not a repository name"),
    ],
)
def test_anything_else_fails(tmp_path: Path, repositories: str, permissions: str, message: str) -> None:
    code, out, got = _check(tmp_path, repositories, permissions)
    assert code != 0
    assert message in out
    assert got == {}
