# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""fetch_deps: a needs-python wheel is installed into the consumer's interpreter, never sys.executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
import yaml
from click.testing import CliRunner
from conftest import REPO_ROOT

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


def test_needs_python_without_consumer_python_fails_loud() -> None:
    result = CliRunner().invoke(fetch_deps.main, ["--deps-json", json.dumps([_DEP])])
    assert result.exit_code != 0
    assert "cxxmath-python" in result.output
    assert "--consumer-python" in result.output
    assert "install-python-deps" in result.output


@pytest.mark.parametrize(("install_python_deps", "exported"), [("true", True), ("false", False)])
def test_compose_exports_lib_dirs_only_for_deps_that_run_here(
    tmp_path: Path, install_python_deps: str, exported: bool
) -> None:
    """Staged HPC deps stay off LD_LIBRARY_PATH: an icpx-built liblz4 there broke the container's zstd."""
    doc = yaml.safe_load((REPO_ROOT / "actions" / "fetch-deps" / "action.yml").read_text(encoding="utf-8"))
    (step,) = [s for s in doc["runs"]["steps"] if s.get("id") == "compose-cmake"]
    dep = tmp_path / "lz4"
    (dep / "lib").mkdir(parents=True)
    files = {k: tmp_path / k for k in ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH")}
    for f in files.values():
        f.touch()
    env = {
        "PATH": os.environ["PATH"],
        "CI_INFRASTRUCTURE_PYTHON": sys.executable,
        "DEPS_JSON": json.dumps([{"install-path": str(dep), "add-to-path": ["bin"]}]),
        "INSTALL_PYTHON_DEPS": install_python_deps,
        **{k: str(v) for k, v in files.items()},
    }
    subprocess.run(["bash", "-c", step["run"]], env=env, check=True)
    assert files["GITHUB_OUTPUT"].read_text(encoding="utf-8") == f"cmake-prefix-path={dep}\n"
    assert (f"LD_LIBRARY_PATH={dep}/lib" in files["GITHUB_ENV"].read_text(encoding="utf-8")) is exported
    assert (f"{dep}/bin" in files["GITHUB_PATH"].read_text(encoding="utf-8")) is exported
