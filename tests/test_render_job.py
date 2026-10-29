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
from release_fixtures import write_release_repo

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


# --- --release (CD v3) ---------------------------------------------------------------------------------------

_RELEASE_LEG_TITLE = "release-rpm (rocky-9, g++-11, release)"


def test_release_renders_the_leg_as_the_gate_hands_it_over(tmp_path: Path) -> None:
    manifest = write_release_repo(tmp_path)
    out = _run(
        "--manifest",
        str(manifest),
        "--leg",
        _RELEASE_LEG_TITLE,
        "--release",
        "prerelease",
        "--tag",
        "2.5.0-RC1",
        "--sha",
        "f" * 40,
    )
    lines = out.splitlines()
    assert lines[1] == f"# pkg/{_RELEASE_LEG_TITLE} [prerelease 2.5.0-RC1]"
    assert "# ci-infrastructure: release overlay" in lines
    assert 'set(CPACK_PACKAGE_VERSION "2.5.0~rc1" CACHE STRING "" FORCE)' in lines
    assert '  "job_key": "pkg-2.5.0-rc1-rpm-rocky-9-gxx-11-ffffffff",' in lines
    subprocess.run(["bash", "-n"], input=out, text=True, check=True)


def test_release_rehearsal_defaults_to_the_repos_head(tmp_path: Path) -> None:
    manifest = write_release_repo(tmp_path)
    out = _run("--manifest", str(manifest), "--leg", _RELEASE_LEG_TITLE, "--release", "rehearsal")
    assert '  "mode": "rehearsal",' in out.splitlines()
    assert "pkg-2.5.0-1.x86_64.rpm" in out


def _fails(*args: str) -> str:
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code != 0
    return result.output


def test_release_refusals(tmp_path: Path) -> None:
    m = str(write_release_repo(tmp_path))
    assert "bump VERSION" in _fails(
        "--manifest", m, "--leg", _RELEASE_LEG_TITLE, "--release", "prerelease", "--tag", "2.5.1-rc1"
    )
    assert "is a release, not a prerelease" in _fails(
        "--manifest", m, "--leg", _RELEASE_LEG_TITLE, "--release", "prerelease", "--tag", "2.5.0"
    )
    assert "needs --tag" in _fails("--manifest", m, "--leg", _RELEASE_LEG_TITLE, "--release", "prerelease")
    assert "takes no --tag" in _fails(
        "--manifest", m, "--leg", _RELEASE_LEG_TITLE, "--release", "rehearsal", "--tag", "2.5.0"
    )
    assert "renders a release leg" in _fails(
        "--manifest", m, "--leg", "build (rocky-9, g++-11)", "--release", "rehearsal"
    )
    assert "go with --release" in _fails("--manifest", m, "--leg", "build (rocky-9, g++-11)", "--tag", "2.5.0")
