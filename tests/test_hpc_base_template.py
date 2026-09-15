# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The shared base recipe, and a repo recipe that extends it."""

from __future__ import annotations

import hashlib
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Final

import pytest

from ci_infrastructure import _github_api
from ci_infrastructure.hpc import jobscript

BASE: Final = "ci-infrastructure/cmake-build.sh.j2"
EXTENDS: Final = f'{{% extends "{BASE}" %}}\n'

LEG: dict[str, Any] = {
    "build-type": "RelWithDebInfo",
    "platform": "hpc-atos-gnu",
    "modules": ["load prgenv/gnu", "load cmake"],
    "cc": "gcc",
    "cxx": "g++",
}


def _render(source: str = EXTENDS, leg: dict[str, Any] | None = None, search_path: Path | None = None) -> str:
    return jobscript.render_job_template(
        template_source=source,
        template_name="build.sh.j2",
        leg={**LEG, **(leg or {})},
        search_path=search_path,
    )


def _base_source() -> bytes:
    return (resources.files("ci_infrastructure.hpc") / "templates" / "cmake-build.sh.j2").read_bytes()


def test_bare_extends_is_a_complete_recipe() -> None:
    out = _render()
    lines = out.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert {"#SBATCH --ntasks=8", "#SBATCH --time=01:00:00", "#SBATCH --gres=ssdtmp:20G"} <= set(lines)
    assert "module load prgenv/gnu" in lines
    assert 'cmake --preset ci -S "$CI_SOURCE_DIR" -B "$build" $gen_flag \\' in lines
    assert "  -DCMAKE_BUILD_TYPE=RelWithDebInfo \\" in lines
    assert "Fortran" not in out
    assert 'ctest --test-dir "$build" --output-on-failure -j "$jobs"' in lines
    assert 'tar -cf - -C "$install_root" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"' in lines


def test_sbatch_block_stays_in_the_wrapped_header() -> None:
    wrapped = jobscript.render_job_script(
        repo_script=_render(), output_path="/o", cmake_prefix_path="/p", install_path="/i"
    ).splitlines()
    assert wrapped.count("#!/bin/bash") == 1
    assert wrapped.index("#SBATCH --ntasks=8") < wrapped.index("#SBATCH --output=/o")
    assert wrapped.index("#SBATCH --output=/o") < wrapped.index("set -euo pipefail")


def test_rendered_recipe_is_valid_bash(tmp_path: Path) -> None:
    script = tmp_path / "job.sh"
    script.write_text(_render(leg={"fc": "gfortran", "options": "with-geo", "ctest-args": "-L nightly -j 8"}))
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_leg_values_beat_the_defaults() -> None:
    lines = _render(leg={"ntasks": 2, "time": "00:40:00", "ssdtmp": "10G"}).splitlines()
    assert {"#SBATCH --ntasks=2", "#SBATCH --time=00:40:00", "#SBATCH --gres=ssdtmp:10G"} <= set(lines)


@pytest.mark.parametrize(("options", "preset"), [("", "ci"), ("with-geo", "with-geo")])
def test_options_name_the_configure_preset(options: str, preset: str) -> None:
    assert f"cmake --preset {preset} -S" in _render(leg={"options": options})


def test_fc_adds_the_fortran_compiler() -> None:
    assert '  -DCMAKE_Fortran_COMPILER="$(command -v gfortran)" \\' in _render(leg={"fc": "gfortran"}).splitlines()


def test_ctest_args_replace_the_default_parallelism() -> None:
    lines = _render(leg={"ctest-args": "-L nightly -j 8"}).splitlines()
    assert 'ctest --test-dir "$build" --output-on-failure -L nightly -j 8' in lines


def test_tests_false_runs_no_ctest_even_when_a_child_overrides_the_block() -> None:
    child = EXTENDS + "{% block test %}\nctest custom\n{% endblock %}\n"
    assert "ctest" not in _render(leg={"tests": False})
    assert "ctest custom" not in _render(child, leg={"tests": False})
    assert "ctest custom" in _render(child)


def test_child_overrides_blocks_and_keeps_the_base_with_super() -> None:
    child = EXTENDS + (
        "{% block preflight %}\n{{ super() }}\necho extra\n{% endblock %}\n"
        "{% block cmake_args %}\n  -DBoost_ROOT=/opt/boost \\\n{% endblock %}\n"
        "{% block install %}\ninstall_root=/stage\n{% endblock %}\n"
    )
    out = _render(child)
    assert "echo extra" in out
    assert "Using: $(command -v gcc)" in out
    assert "  -DBoost_ROOT=/opt/boost \\\n  -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON \\\n" in out
    assert "install_root=/stage" in out
    assert 'cmake --install "$build"' not in out


def test_static_check_follows_extends() -> None:
    assert jobscript.undeclared_template_names(EXTENDS, LEG, template_name="t") == set()
    without_cxx = {k: v for k, v in LEG.items() if k != "cxx"}
    assert jobscript.undeclared_template_names(EXTENDS, without_cxx, template_name="t") == {"cxx"}


def test_static_check_sees_a_child_block_and_not_super() -> None:
    src = EXTENDS + "{% block preflight %}{{ super() }}{{ boost_root }}{% endblock %}\n"
    assert jobscript.undeclared_template_names(src, LEG, template_name="t") == {"boost_root"}


def test_static_check_follows_include_from_the_recipe_directory(tmp_path: Path) -> None:
    (tmp_path / "_part.sh.j2").write_text("{{ nope }}\n")
    src = '{% include "_part.sh.j2" %}\n'
    assert jobscript.undeclared_template_names(src, LEG, template_name="t", search_path=tmp_path) == {"nope"}


def test_unknown_base_template_is_a_named_error() -> None:
    src = '{% extends "ci-infrastructure/nope.sh.j2" %}\n'
    with pytest.raises(jobscript.JobTemplateError, match="nope.sh.j2"):
        jobscript.undeclared_template_names(src, LEG, template_name="t")
    with pytest.raises(jobscript.JobTemplateError, match="nope.sh.j2"):
        _render(src)


def test_computed_template_name_is_refused() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="computed"):
        jobscript.undeclared_template_names("{% extends cc %}\n", LEG, template_name="t")


def test_repo_file_cannot_shadow_the_base(tmp_path: Path) -> None:
    shadow = tmp_path / "ci-infrastructure" / "cmake-build.sh.j2"
    shadow.parent.mkdir()
    shadow.write_text("shadowed\n")
    assert "shadowed" not in _render(search_path=tmp_path)


TEMPLATE_SHA256: Final = "ea812388d9d782220baa94311ee215c1f9f5ac56e83c729c7e0615aa409ad1c2"
TEMPLATE_VERSION: Final = 0


def test_base_template_change_bumps_the_template_version() -> None:
    """Consumers load ci-infrastructure @main, so an edit to the base reaches every repo
    without moving a sha; only the version in the artifact name makes them rebuild."""
    assert (hashlib.sha256(_base_source()).hexdigest(), _github_api.HPC_TEMPLATE_VERSION) == (
        TEMPLATE_SHA256,
        TEMPLATE_VERSION,
    ), "cmake-build.sh.j2 changed: bump _github_api.HPC_TEMPLATE_VERSION and update both pins"
