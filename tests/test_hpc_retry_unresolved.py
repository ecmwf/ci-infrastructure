# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ci_infrastructure.hpc import site
from ci_infrastructure.hpc.retry_unresolved import run


def _tool(tmp_path: Path, failures: int, message: str) -> list[str]:
    """A command that fails `failures` times with `message` on stderr, then prints ok."""
    count = tmp_path / "count"
    script = tmp_path / "tool.sh"
    script.write_text(
        f"n=$(cat {count} 2>/dev/null || echo 0); echo $((n + 1)) > {count}\n"
        f'if [ "$n" -lt {failures} ]; then echo "{message}" >&2; exit 255; fi\necho ok\n'
    )
    return ["sh", str(script)]


def _calls(tmp_path: Path) -> int:
    return int((tmp_path / "count").read_text())


def test_an_unresolved_host_is_retried(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    argv = _tool(tmp_path, 2, "ssh: Could not resolve hostname hpc-batch: Name or service not known")
    assert run(argv, delays=(0, 0)) == 0
    assert _calls(tmp_path) == 3
    out, err = capfd.readouterr()
    assert out == "ok\n"
    assert err.count("retrying in 0s") == 2


def test_it_gives_up_after_the_last_delay(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    argv = _tool(tmp_path, 5, "ssh: Could not resolve hostname hpc-batch: Name or service not known")
    assert run(argv, delays=(0, 0)) == 255
    assert _calls(tmp_path) == 3
    assert capfd.readouterr().err.endswith("Could not resolve hostname hpc-batch: Name or service not known\n")


def test_other_failures_are_not_retried(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    argv = _tool(tmp_path, 5, "Permission denied (publickey).")
    assert run(argv, delays=(0, 0)) == 255
    assert _calls(tmp_path) == 1
    assert capfd.readouterr().err == "Permission denied (publickey).\n"


def test_a_stalled_command_is_killed_and_retried(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    count = tmp_path / "count"
    script = tmp_path / "stall.sh"
    script.write_text(
        f"n=$(cat {count} 2>/dev/null || echo 0); echo $((n + 1)) > {count}\n"
        f'if [ "$n" -lt 1 ]; then exec sleep 60; fi\necho ok\n'
    )
    assert run(["sh", str(script)], delays=(0,), timeout=0.5) == 0
    assert _calls(tmp_path) == 2
    out, err = capfd.readouterr()
    assert out == "ok\n"
    assert "no result after 0.5s; retrying in 0s" in err


def test_a_command_that_keeps_stalling_fails(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    assert run(["sleep", "60"], delays=(0,), timeout=0.2) != 0
    assert capfd.readouterr().err.endswith("sleep: no result after 0.2s; giving up\n")


def test_the_connection_uses_the_wrappers_beside_this_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ci-infrastructure-ssh").touch()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    conn = SimpleNamespace(ssh="ssh", scp="scp")
    site._retry_unresolved(conn)
    assert conn.ssh == str(tmp_path / "ci-infrastructure-ssh")
    assert conn.scp == "scp"
