# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""mint-token's check step: what each App may grant, and the inputs handed to create-github-app-token."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import action_run_body


def _check(tmp_path: Path, app: str, repositories: str, permissions: str) -> tuple[int, str, dict[str, str]]:
    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "APP": app,
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


def test_a_reader_token_names_its_repositories_and_permissions(tmp_path: Path) -> None:
    code, out, got = _check(tmp_path, "reader", "eckit, ecmwf/eccodes\nfdb", "contents:read,actions:read")
    assert code == 0, out
    assert got == {
        "actions": "read",
        "administration": "",
        "checks": "",
        "contents": "read",
        "pull-requests": "",
        "repositories": "eckit,eccodes,fdb",
    }


def test_the_dispatcher_may_write_actions(tmp_path: Path) -> None:
    code, out, got = _check(tmp_path, "dispatcher", "atlas", "actions:write")
    assert code == 0, out
    assert got["actions"] == "write"


def test_synced_pr_links_may_take_every_installed_repository(tmp_path: Path) -> None:
    code, out, got = _check(tmp_path, "synced-pr-links", "*", "pull-requests:write")
    assert code == 0, out
    assert got["repositories"] == ""
    assert got["pull-requests"] == "write"


@pytest.mark.parametrize(
    ("app", "repositories", "permissions", "message"),
    [
        ("reader", "eckit", "contents:write", "reader may not grant 'contents:write'"),
        ("reader", "eckit", "statuses:read", "may not grant 'statuses:read'"),
        ("reporter", "eckit", "actions:write", "reporter may not grant"),
        ("ci-permissions", "eckit", "contents:read", "app must be"),
        ("reader", "eckit", "", "permissions is empty"),
        ("reader", "", "contents:read", "repositories is empty"),
        ("reader", "other/eckit", "contents:read", "not a repository of ecmwf"),
        ("reader", "eck$it", "contents:read", "not a repository name"),
        ("reader", "*", "contents:read", "only synced-pr-links may take"),
        ("dispatcher", "*", "actions:write", "only synced-pr-links may take"),
        ("synced-pr-links", "*", "", "permissions is empty"),
    ],
)
def test_anything_else_fails(tmp_path: Path, app: str, repositories: str, permissions: str, message: str) -> None:
    code, out, got = _check(tmp_path, app, repositories, permissions)
    assert code != 0
    assert message in out
    assert got == {}
