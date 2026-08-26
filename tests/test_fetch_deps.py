# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the consumer-interpreter preflight in fetch_deps.

`needs-python` deps ship a wheel that has to be installed into the *consumer's*
interpreter, never into the ci-infrastructure helper venv running this script.
Two behaviours are pinned here:

  * A leg that means to install (the default) and hasn't got an interpreter
    fails loudly rather than falling back to sys.executable.
  * A leg that installs the wheels elsewhere — an HPC leg, whose job script pip
    installs them on the compute node — passes --no-python-install and gets the
    wheels staged with no interpreter demanded and nothing installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from click.testing import CliRunner

from ci_infrastructure import fetch_deps

_DEP: Final = {
    "name": "cxxmath-python",
    "repo": "org/downstream-ci-repo-cxx-py",
    "ref": "main",
    "sha": "deadbeef",
    "artifact-name": "cxxmath-python-hpc-atos-gnu-Release-deadbeef",
    "cached": True,
    "source": "store",
    "install-path": "/tmp/install/cxxmath-python",
    "needs-python": True,
}


def _run(*args: str) -> Any:
    return CliRunner().invoke(fetch_deps.main, ["--deps-json", json.dumps([_DEP]), *args])


def test_needs_python_without_consumer_python_fails_loud() -> None:
    result = _run()
    assert result.exit_code != 0
    # CIError is a ClickException: click renders it via show() and exits 1, so
    # the text lands in the captured output rather than on the exception.
    message = result.output
    assert "cxxmath-python" in message
    # The message must name both ways out, since which one is right depends on
    # whether the leg tests here or on a compute node.
    assert "--consumer-python" in message
    assert "install-python-deps" in message


def test_no_python_install_stages_wheels_without_an_interpreter(monkeypatch: Any) -> None:
    installed: list[Path] = []

    def _record(install_path: Path, consumer_python: Path) -> bool:
        installed.append(install_path)
        return True

    monkeypatch.setattr(fetch_deps, "select_token", lambda: "t0ken")
    monkeypatch.setattr(fetch_deps, "_download_and_report", lambda dep: True)
    monkeypatch.setattr(fetch_deps, "pip_install_wheel", _record)

    result = _run("--no-python-install")

    assert result.exit_code == 0, result.output
    # Staged, not installed: the compute node does the installing.
    assert installed == []
    assert "staging needs-python wheels" in result.output
