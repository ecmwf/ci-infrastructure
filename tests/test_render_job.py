# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""ci-infrastructure-render: the standalone script CI runs and people rerun."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner
from conftest import write_repo

from ci_infrastructure.render_job import main

_MANIFEST = """
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
    platform = "ubuntu-24.04"
    build-type = "Release"
    c-compiler = "gcc-13"
    cxx-compiler = "g++-13"
    container = "img:latest"
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
    platform = "ubuntu-24.04"
    build-type = "Debug"
    c-compiler = "gcc-13"
    cxx-compiler = "g++-13"
    [matrix.build]
    defaults.job-script = "./.ci/build.sh.j2"
    triggers = ["rebuild-request"]
    needs = []

    [[matrix.build-hpc.include]]
    platform = "hpc-atos-gnu"
    build-type = "Release"
    modules = ["load cmake"]
    c-compiler-binary = "gcc"
    cxx-compiler-binary = "g++"
    [matrix.build-hpc]
    execution = "hpc-atos"
    defaults.job-script = "./.ci/hpc/build.sh.j2"
    triggers = ["rebuild-request"]
    needs = []
"""


def _repo(tmp_path: Path) -> Path:
    manifest = write_repo(tmp_path, "pkg", _MANIFEST)
    (manifest.parent / "build.sh.j2").write_text('{% extends "ci-infrastructure/cmake-runner.sh.j2" %}\n')
    (manifest.parent / "hpc").mkdir()
    (manifest.parent / "hpc" / "build.sh.j2").write_text('{% extends "ci-infrastructure/cmake-atos.sh.j2" %}\n')
    return manifest


def _run(*args: str) -> str:
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_list_shows_the_job_titles(tmp_path: Path) -> None:
    assert _run("--manifest", str(_repo(tmp_path)), "--list").splitlines() == [
        "pkg/build (ubuntu-24.04, Release)",
        "pkg/build (ubuntu-24.04, Debug)",
        "pkg/build-hpc (hpc-atos-gnu, g++)",
    ]


def test_leg_by_title_renders_a_runnable_runner_script(tmp_path: Path) -> None:
    out = _run("--manifest", str(_repo(tmp_path)), "--leg", "build (ubuntu-24.04, Release)")
    lines = out.splitlines()
    assert lines[:4] == [
        "#!/bin/bash",
        "# pkg/build (ubuntu-24.04, Release)",
        "# Container: img:latest",
        "set -euo pipefail",
    ]
    assert 'export CI_INSTALL_PREFIX="${CI_INSTALL_PREFIX:-$PWD/_ci/install}"' in lines
    assert not [line for line in lines if line.startswith("#SBATCH")]
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)


def test_hpc_leg_keeps_the_sbatch_header_first(tmp_path: Path) -> None:
    lines = _run("--manifest", str(_repo(tmp_path)), "--leg", "pkg/build-hpc (hpc-atos-gnu, g++)").splitlines()
    assert lines[1].startswith("#SBATCH")
    assert lines.index("set -euo pipefail") > max(i for i, line in enumerate(lines) if line.startswith("#SBATCH"))
    assert any(line.startswith('export CI_INSTALL_ARCHIVE="${CI_INSTALL_ARCHIVE:-') for line in lines)


def test_matrix_leg_json_is_how_ci_calls_it(tmp_path: Path) -> None:
    recipe = _repo(tmp_path).parent / "build.sh.j2"
    leg = {"build-type": "Release", "c-compiler": "gcc-13", "cxx-compiler": "g++-13"}
    out_file = tmp_path / "job.sh"
    _run("--job-script", str(recipe), "--matrix-leg", json.dumps(leg), "-o", str(out_file))
    assert out_file.stat().st_mode & 0o111
    assert '-DCMAKE_CXX_COMPILER="$(command -v g++-13)"' in out_file.read_text()


def test_unknown_leg_lists_the_titles(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--manifest", str(_repo(tmp_path)), "--leg", "build (nope)"])
    assert result.exit_code != 0
    assert "pkg/build (ubuntu-24.04, Debug)" in result.output
