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

BASE: Final = "ci-infrastructure/cmake-atos.sh.j2"
EXTENDS: Final = f'{{% extends "{BASE}" %}}\n'

LEG: dict[str, Any] = {
    "build-type": "RelWithDebInfo",
    "platform": "hpc-atos-gnu",
    "modules": ["load prgenv/gnu", "load cmake"],
    "c-compiler": "gcc",
    "cxx-compiler": "g++",
}


def _render(source: str = EXTENDS, leg: dict[str, Any] | None = None, search_path: Path | None = None) -> str:
    return jobscript.render_job_template(
        template_source=source,
        template_name="build.sh.j2",
        leg={**LEG, **(leg or {})},
        execution="hpc-atos",
        search_path=search_path,
    )


def _base_source() -> bytes:
    templates = resources.files("ci_infrastructure") / "templates"
    return b"".join(
        (templates / name).read_bytes()
        for name in ("cmake-runner.sh.j2", "cmake-atos.sh.j2", "cmake-all-lanes.sh.j2", "python.j2")
    )


def test_bare_extends_is_a_complete_recipe() -> None:
    out = _render()
    lines = out.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert lines[1:6] == [
        "#SBATCH --qos=nf",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=8",
        "#SBATCH --gres=ssdtmp:20G",
        "#SBATCH --time=01:00:00",
    ]
    assert 'jobs="${SLURM_CPUS_PER_TASK:-${SLURM_NTASKS:-8}}"' in lines
    assert "module load prgenv/gnu" in lines
    assert 'cmake --preset ci -S "$CI_SOURCE_DIR" -B "$build" $gen_flag \\' in lines
    assert "  -DCMAKE_BUILD_TYPE=RelWithDebInfo \\" in lines
    assert "Fortran" not in out
    assert 'ctest --test-dir "$build" --output-on-failure -j "$jobs"' in lines
    assert 'tar -cf - -C "$install_root" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"' in lines


def test_cpus_per_task_and_mem_render_only_when_set() -> None:
    bare = _render().splitlines()
    assert not [line for line in bare if line.startswith(("#SBATCH --cpus-per-task", "#SBATCH --mem"))]
    lines = _render(leg={"cpus-per-task": 64, "mem": "64GB"}).splitlines()
    assert lines[lines.index("#SBATCH --ntasks=8") + 1 : lines.index("#SBATCH --gres=ssdtmp:20G")] == [
        "#SBATCH --cpus-per-task=64",
        "#SBATCH --mem=64GB",
    ]


def test_sbatch_block_stays_in_the_wrapped_header() -> None:
    wrapped = jobscript.render_job_script(
        repo_script=_render(), output_path="/o", cmake_prefix_path="/p", install_path="/i"
    ).splitlines()
    assert wrapped.count("#!/bin/bash") == 1
    assert wrapped.index("#SBATCH --ntasks=8") < wrapped.index("#SBATCH --output=/o")
    assert wrapped.index("#SBATCH --output=/o") < wrapped.index("set -euo pipefail")


def test_rendered_recipe_is_valid_bash(tmp_path: Path) -> None:
    script = tmp_path / "job.sh"
    script.write_text(
        _render(leg={"fortran-compiler": "gfortran", "options": "with-geo", "ctest-args": "-L nightly -j 8"})
    )
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_leg_values_beat_the_defaults() -> None:
    leg = {"qos": "np", "nodes": 2, "ntasks": 2, "time": "00:40:00", "ssdtmp": "10G"}
    lines = _render(leg=leg).splitlines()
    assert {
        "#SBATCH --qos=np",
        "#SBATCH --nodes=2",
        "#SBATCH --ntasks=2",
        "#SBATCH --time=00:40:00",
        "#SBATCH --gres=ssdtmp:10G",
    } <= set(lines)
    assert 'jobs="${SLURM_CPUS_PER_TASK:-${SLURM_NTASKS:-2}}"' in lines


def test_a_repo_block_sees_the_defaulted_ntasks() -> None:
    src = EXTENDS + "{% block preflight %}echo {{ ntasks }}{% endblock %}\n"
    assert "echo 8" in _render(src).splitlines()
    assert jobscript.undeclared_template_names(src, LEG, template_name="t") == set()


@pytest.mark.parametrize(("options", "preset"), [("", "ci"), ("with-geo", "with-geo")])
def test_options_name_the_configure_preset(options: str, preset: str) -> None:
    assert f"cmake --preset {preset} -S" in _render(leg={"options": options})


def test_fortran_compiler_adds_the_fortran_compiler() -> None:
    leg = {"fortran-compiler": "gfortran"}
    assert '  -DCMAKE_Fortran_COMPILER="$(command -v gfortran)" \\' in _render(leg=leg).splitlines()


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
    without_cxx = {k: v for k, v in LEG.items() if k != "cxx-compiler"}
    assert jobscript.undeclared_template_names(EXTENDS, without_cxx, template_name="t") == {"cxx_compiler_binary"}


@pytest.mark.parametrize("base", ["cmake-runner.sh.j2", "cmake-atos.sh.j2"])
def test_set_environment_runs_after_setup_and_before_preflight(base: str) -> None:
    src = f'{{% extends "ci-infrastructure/{base}" %}}\n{{% block set_environment %}}\nsource venv\n{{% endblock %}}\n'
    lines = _render(src).splitlines()
    assert (
        lines.index('gen_flag=""')
        < lines.index("source venv")
        < lines.index('echo "Using: $(command -v gcc) ($(gcc --version | head -1))"')
    )


def test_static_check_accepts_a_name_read_through_default() -> None:
    src = "{{ a | default(1) }}{{ b }}\n"
    assert jobscript.undeclared_template_names(src, LEG, template_name="t") == {"b"}


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
    shadow = tmp_path / "ci-infrastructure" / "cmake-atos.sh.j2"
    shadow.parent.mkdir()
    shadow.write_text("shadowed\n")
    assert "shadowed" not in _render(search_path=tmp_path)


RUNNER_LEG: dict[str, Any] = {
    "build-type": "Release",
    "c-compiler": "gcc-13",
    "cxx-compiler": "g++-13",
    "fortran-compiler": "gfortran-13",
}
RUNNER: Final = '{% extends "ci-infrastructure/cmake-runner.sh.j2" %}\n'


def _render_runner(source: str = RUNNER, leg: dict[str, Any] | None = None) -> str:
    return jobscript.render_job_template(
        template_source=source, template_name="build.sh.j2", leg=leg or RUNNER_LEG, execution="runner"
    )


def test_runner_base_has_no_slurm_parts() -> None:
    out = _render_runner()
    assert out.startswith("#!/bin/bash\n")
    assert not [line for line in out.splitlines() if line.startswith(("#SBATCH", "module ", "tar "))]
    assert 'build="${CI_BUILD_DIR:-${TMPDIR:-/tmp}/build}"' in out
    assert 'cmake --install "$build"' in out


def test_runner_uses_cmakes_default_generator_and_hpc_ninja() -> None:
    assert "-GNinja" not in _render_runner()
    assert 'if command -v ninja >/dev/null 2>&1; then gen_flag="-GNinja"; fi' in _render()


def test_compiler_fields_name_the_compilers() -> None:
    out = _render_runner()
    assert '  -DCMAKE_C_COMPILER="$(command -v gcc-13)" \\' in out
    assert '  -DCMAKE_Fortran_COMPILER="$(command -v gfortran-13)" \\' in out
    assert jobscript.undeclared_template_names(RUNNER, RUNNER_LEG, template_name="t") == set()
    without_cc = {k: v for k, v in RUNNER_LEG.items() if k != "c-compiler"}
    assert jobscript.undeclared_template_names(RUNNER, without_cc, template_name="t") == {"c_compiler_binary"}


TEMPLATE_SHA256: Final = "099770b804f8c4410e8d22582de28e68eb5c65861fdd410047bc92a13b991722"
TEMPLATE_VERSION: Final = 2


def test_base_template_change_bumps_the_template_version() -> None:
    """Consumers load ci-infrastructure @main, so an edit to the base reaches every repo
    without moving a sha; only the version in the artifact name makes them rebuild."""
    assert (hashlib.sha256(_base_source()).hexdigest(), _github_api.HPC_TEMPLATE_VERSION) == (
        TEMPLATE_SHA256,
        TEMPLATE_VERSION,
    ), "a shared CMake template changed: bump _github_api.HPC_TEMPLATE_VERSION and update both pins"


ALL_LANES: Final = '{% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}\n'


def test_all_lanes_extends_the_template_of_the_lane() -> None:
    assert _render_runner(ALL_LANES) == _render_runner()
    assert _render(ALL_LANES) == _render()


def test_all_lanes_overrides_once_for_both_lanes() -> None:
    src = ALL_LANES + "{% block build %}echo build on {{ execution }}{% endblock %}\n"
    assert "echo build on runner" in _render_runner(src)
    assert "echo build on hpc-atos" in _render(src)


def test_a_lane_without_a_branch_renders_nothing_and_fails() -> None:
    with pytest.raises(jobscript.JobTemplateError, match="renders nothing for execution = 'hpc-lumi'"):
        jobscript.render_job_template(
            template_source=ALL_LANES,
            template_name="t",
            leg=LEG,
            execution="hpc-lumi",  # type: ignore[arg-type]
        )


def test_static_check_follows_both_lanes_of_all_lanes() -> None:
    assert jobscript.undeclared_template_names(ALL_LANES, RUNNER_LEG, template_name="t") == set()
    assert jobscript.undeclared_template_names(ALL_LANES, LEG, template_name="t") == set()


def test_the_compiler_binary_defaults_to_the_compiler_and_may_differ() -> None:
    assert "$(command -v g++-13)" in _render_runner()
    hpc_leg = {**LEG, "cxx-compiler": "g++-8", "cxx-compiler-binary": "g++"}
    assert '-DCMAKE_CXX_COMPILER="$(command -v g++)"' in _render(leg=hpc_leg)


def test_cc_no_longer_names_the_binary() -> None:
    leg = {**{k: v for k, v in LEG.items() if k != "c-compiler"}, "cc": "gcc"}
    assert jobscript.undeclared_template_names(EXTENDS, leg, template_name="t") == {"c_compiler_binary"}


PY_RECIPE: Final = (
    '{% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}\n'
    '{% import "ci-infrastructure/python.j2" as py %}\n'
    "{% block set_environment %}\n"
    "{{ py.get_python_via_uv(python_version) }}\n"
    "{% endblock %}\n"
)


def _runner_with(leg: dict[str, Any], source: str = PY_RECIPE) -> str:
    return _render_runner(source, leg={**RUNNER_LEG, **leg})


def _atos_with(leg: dict[str, Any], source: str = PY_RECIPE) -> str:
    return _render(source, leg=leg)


@pytest.mark.parametrize("render", [_runner_with, _atos_with], ids=["runner", "atos"])
def test_get_python_via_uv_creates_and_activates_a_venv(render: Any) -> None:
    lines = render({"python-version": "3.11"}).splitlines()
    venv = '"${TMPDIR:-/tmp}/ci-python"'
    assert f"UV_PYTHON_PREFERENCE=only-managed uv venv --clear --python 3.11 {venv}" in lines
    assert '. "${TMPDIR:-/tmp}/ci-python/bin/activate"' in lines


@pytest.mark.parametrize("render", [_runner_with, _atos_with], ids=["runner", "atos"])
def test_without_the_macro_nothing_about_python_renders(render: Any) -> None:
    out = render({"python-version": "3.11"}, EXTENDS if render is _atos_with else RUNNER)
    assert "uv venv" not in out
    assert "Python3_EXECUTABLE" not in out


@pytest.mark.parametrize("render", [_runner_with, _atos_with], ids=["runner", "atos"])
def test_a_numeric_python_version_is_rejected(render: Any) -> None:
    with pytest.raises(jobscript.JobTemplateError, match="python-version must be a string"):
        render({"python-version": 3.1})


@pytest.mark.parametrize("render", [_runner_with, _atos_with], ids=["runner", "atos"])
def test_get_python_via_uv_renders_valid_bash(render: Any, tmp_path: Path) -> None:
    script = tmp_path / "job.sh"
    script.write_text(render({"python-version": "3.12"}))
    subprocess.run(["bash", "-n", str(script)], check=True)
