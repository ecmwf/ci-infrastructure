# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""python-{runner,atos,all-lanes}.sh.j2: a wheel, installed with the deps' wheels, then pytest."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from ci_infrastructure._github_api import Execution
from ci_infrastructure.hpc import jobscript
from ci_infrastructure.render_job import wrap

RUNNER: Final = '{% extends "ci-infrastructure/python-runner.sh.j2" %}\n'
ATOS: Final = '{% extends "ci-infrastructure/python-atos.sh.j2" %}\n'
ALL_LANES: Final = '{% extends "ci-infrastructure/python-all-lanes.sh.j2" %}\n'

LEG: Final[dict[str, Any]] = {"platform": "ubuntu-24.04", "python-version": "3.12"}


def _render(source: str = RUNNER, leg: dict[str, Any] | None = None, execution: Execution = "runner") -> str:
    return jobscript.render_job_template(
        template_source=source, template_name="python.sh.j2", leg={**LEG, **(leg or {})}, execution=execution
    )


def test_runner_builds_installs_and_tests_the_wheel() -> None:
    out = _render()
    assert "UV_PYTHON_PREFERENCE=only-managed uv venv --clear --python 3.12" in out
    assert out.index('uv build --wheel --out-dir "$wheels" "$CI_SOURCE_DIR"') < out.index("uv pip install")
    assert '"${own_wheels[0]}" pytest\n' in out
    assert out.rstrip().endswith("python -m pytest")
    assert "cmake" not in out and "#SBATCH" not in out


def test_extras_and_pytest_args_come_from_the_leg() -> None:
    out = _render(leg={"extras": "all,tests", "pytest-args": "-n auto -m 'not slow'"})
    assert '"${own_wheels[0]}[all,tests]" pytest\n' in out
    assert "python -m pytest -n auto -m 'not slow'\n" in out


def test_without_tests_nothing_runs_pytest() -> None:
    out = _render(leg={"tests": False})
    assert "pytest" not in out


def test_without_python_version_the_venv_takes_uvs_python() -> None:
    leg = {k: v for k, v in LEG.items() if k != "python-version"}
    out = jobscript.render_job_template(template_source=RUNNER, template_name="t", leg=leg, execution="runner")
    assert 'uv venv --clear "${TMPDIR:-/tmp}/ci-python"' in out and "--python" not in out


def test_a_recipe_overrides_the_test_requirements() -> None:
    out = _render(RUNNER + "{% block test_requirements %}pytest pytest-xdist{% endblock %}\n")
    assert '"${own_wheels[0]}" pytest pytest-xdist\n' in out


def test_atos_adds_the_sbatch_header_modules_and_archive() -> None:
    out = _render(ATOS, {"modules": ["load uv"], "qos": "ng", "gpus-per-task": 1}, execution="hpc-atos")
    lines = out.splitlines()
    assert lines[1:7] == [
        "#SBATCH --qos=ng",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        "#SBATCH --gpus-per-task=1",
        "#SBATCH --mem=16GB",
        "#SBATCH --time=01:00:00",
    ]
    assert "module load uv\n" in out
    assert out.index("python -m pytest") < out.index('zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"')


def test_atos_requests_node_local_ssd_only_when_asked() -> None:
    assert "ssdtmp" not in _render(ATOS, execution="hpc-atos")
    assert "#SBATCH --gres=ssdtmp:30G\n" in _render(ATOS, {"ssdtmp": "30G"}, execution="hpc-atos")


def test_all_lanes_extends_the_template_of_the_lane() -> None:
    assert _render(ALL_LANES) == _render()
    assert _render(ALL_LANES, execution="hpc-atos") == _render(ATOS, execution="hpc-atos")


def test_a_minimal_leg_declares_every_name() -> None:
    for source in (RUNNER, ATOS, ALL_LANES):
        assert jobscript.undeclared_template_names(source, {"platform": "x"}, template_name="t") == set()


def test_the_rendered_scripts_are_valid_bash() -> None:
    for out in (_render(RUNNER), _render(ATOS, execution="hpc-atos")):
        subprocess.run(["bash", "-n"], input=out, text=True, check=True)


def _project(root: Path, name: str, version: str, deps: list[str]) -> Path:
    pkg = name.replace("-", "_")
    (root / "src" / pkg).mkdir(parents=True)
    (root / "src" / pkg / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "[project]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f"dependencies = {deps!r}\n".replace("'", '"')
    )
    return root


def _job_script(tmp_path: Path, leg: dict[str, Any]) -> Path:
    script = tmp_path / "job.sh"
    rendered = jobscript.render_job_template(template_source=RUNNER, template_name="t", leg=leg, execution="runner")
    script.write_text(wrap(rendered, leg=leg, execution="runner"))
    return script


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_dep_wheel_on_the_prefix_path_satisfies_the_requirement(tmp_path: Path) -> None:
    """The upstream's dev wheel is installed, though the index does not have the package."""
    upstream = _project(tmp_path / "upstream", "ci-infra-test-upstream", "9.9.dev1", [])
    subprocess.run(["uv", "build", "-q", "--wheel", "-o", tmp_path / "dep" / "wheels", upstream], check=True)
    project = _project(tmp_path / "project", "ci-infra-test-project", "0.1", ["ci-infra-test-upstream>=1"])
    (project / "tests").mkdir()
    (project / "tests" / "test_import.py").write_text("def test_import():\n    import ci_infra_test_upstream\n")
    # No python-version: the venv takes a Python uv finds, so the test needs no download.
    leg = {"platform": "x"}
    script = _job_script(tmp_path, leg)
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CI_SOURCE_DIR": str(project),
        "CI_INSTALL_PREFIX": str(tmp_path / "install"),
        "CMAKE_PREFIX_PATH": f"{tmp_path / 'missing'};{tmp_path / 'dep'}",
    }
    subprocess.run(["bash", script], cwd=tmp_path, env=env, check=True)
    assert [p.name for p in (tmp_path / "install" / "wheels").glob("*.whl")] == [
        "ci_infra_test_project-0.1-py2.py3-none-any.whl"
    ]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_shallow_checkout_gets_its_version_from_the_tags(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    (origin / "src" / "ci_infra_test_vcs").mkdir(parents=True)
    (origin / "src" / "ci_infra_test_vcs" / "__init__.py").write_text("")
    (origin / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling", "hatch-vcs"]\n'
        'build-backend = "hatchling.build"\n'
        "[project]\n"
        'name = "ci-infra-test-vcs"\n'
        'dynamic = ["version"]\n'
        "[tool.hatch.version]\n"
        'source = "vcs"\n'
    )
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C"]
    subprocess.run(["git", "init", "-q", origin], check=True)
    subprocess.run([*git, origin, "add", "."], check=True)
    subprocess.run([*git, origin, "commit", "-qm", "one"], check=True)
    subprocess.run([*git, origin, "tag", "1.2.3"], check=True)
    subprocess.run([*git, origin, "commit", "-qm", "two", "--allow-empty"], check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", checkout], check=True)
    leg = {"platform": "x", "tests": False}
    script = _job_script(tmp_path, leg)
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CI_SOURCE_DIR": str(checkout),
        "CI_INSTALL_PREFIX": str(tmp_path / "i"),
    }
    subprocess.run(["bash", script], cwd=tmp_path, env=env, check=True)
    (wheel,) = (tmp_path / "i" / "wheels").glob("*.whl")
    assert wheel.name.startswith("ci_infra_test_vcs-1.2.4.dev1"), wheel.name
