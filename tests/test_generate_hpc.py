# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""`execution = "hpc"` kinds in generate_downstream_ci."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from conftest import parse_all, render_single, write_repo

from ci_infrastructure._github_api import EXECUTION_HPC
from ci_infrastructure.generate_downstream_ci import (
    SchemaError,
    parse_manifest,
    validate_graph,
    validate_job_templates,
)

_HPC_MANIFEST: Final = """
    [matrix.build]
    execution = "hpc"
    triggers = ["rebuild-request"]
    job-script = "./.ci/hpc/build.sh"
    forwarded-deps-outputs = ["cmake-prefix-path"]
    needs = []

    [[matrix.build.include]]
    runs-on = "hpc-login-selfhosted"
    site = "hpc-batch"
    compiler = "gnu-12"
    build-type = "Release"
    platform = "hpc-atos-gnu"
    """


@pytest.fixture
def hpc_yaml(tmp_path: Path) -> str:
    return render_single(tmp_path, _HPC_MANIFEST, EXECUTION_HPC)


@pytest.mark.parametrize(
    "line",
    [
        "uses: ecmwf/ci-infrastructure/actions/build-on-hpc@main",
        "matrix.job-script || './.ci/hpc/build.sh'",
        "site: ${{ matrix.site }}",
        "actions/fetch-deps@main",
        "cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}",
        "troika-user: ${{ secrets.HPC_CI_SSH_USER }}",
        "install-python-deps: 'false'",
        "runs-on: ${{ matrix['runs-on'] }}",
        "image: ${{ matrix.container || '' }}",
    ],
)
def test_hpc_job_renders(hpc_yaml: str, line: str) -> None:
    assert line in hpc_yaml


@pytest.mark.parametrize("absent", ["actions/setup-python", "actions/publish-artifact@main", "credentials:"])
def test_hpc_job_omits(hpc_yaml: str, absent: str) -> None:
    assert absent not in hpc_yaml


def test_hpc_job_script_is_per_leg_with_kind_level_fallback(tmp_path: Path) -> None:
    yaml = render_single(
        tmp_path,
        """
        [matrix.build-hpc]
        execution = "hpc"
        triggers = ["rebuild-request"]
        job-script = "./.ci/hpc/build-py3.12.sh"
        forwarded-deps-outputs = ["cmake-prefix-path"]
        needs = []

        [[matrix.build-hpc.include]]
        runs-on = ["self-hosted", "linux", "hpc"]
        site = "hpc-batch"
        python-version = "3.11"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        job-script = "./.ci/hpc/build-py3.11.sh"

        [[matrix.build-hpc.include]]
        runs-on = ["self-hosted", "linux", "hpc"]
        site = "hpc-batch"
        python-version = "3.12"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        job-script = "./.ci/hpc/build-py3.12.sh"
        """,
        EXECUTION_HPC,
    )
    assert "matrix.job-script || './.ci/hpc/build-py3.12.sh'" in yaml
    assert "job-script: ./.ci/hpc/build-py3.12.sh" not in yaml


def test_hpc_test_only_kind_passes_publish_false(tmp_path: Path) -> None:
    yaml = render_single(
        tmp_path,
        """
        [matrix.test-hpc]
        execution = "hpc"
        triggers = ["upstream-change"]
        job-script = "./.ci/hpc/test.sh"
        forwarded-deps-outputs = ["cmake-prefix-path"]
        publishes = false
        needs = []

        [[matrix.test-hpc.include]]
        runs-on = ["self-hosted", "linux", "hpc"]
        site = "hpc-batch"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        job-script = "./.ci/hpc/test.sh"
        """,
        EXECUTION_HPC,
    )
    assert "publish: 'false'" in yaml
    assert "name: Run on HPC" in yaml


def test_container_credentials_are_opt_in(tmp_path: Path) -> None:
    yaml = render_single(
        tmp_path,
        _HPC_MANIFEST.replace('execution = "hpc"', 'execution = "hpc"\ncontainer-credentials = true'),
        EXECUTION_HPC,
    )
    assert "credentials:" in yaml
    assert "username: ${{ secrets.ECCR_PULL_ROBOT_NAME }}" in yaml
    assert "password: ${{ secrets.ECCR_PULL_ROBOT_TOKEN }}" in yaml


def test_legs_differing_only_by_site_collide(tmp_path: Path) -> None:
    """`site` is scheduling, not artifact identity."""
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        execution = "hpc"
        triggers = ["rebuild-request"]
        job-script = "./.ci/hpc/build.sh"
        needs = []

        [[matrix.build.include]]
        runs-on = "hpc-login-selfhosted"
        site = "hpc-batch"
        compiler = "gnu-12"
        build-type = "Release"
        platform = "hpc-atos-gnu"

        [[matrix.build.include]]
        runs-on = "hpc-login-selfhosted"
        site = "lumi"
        compiler = "gnu-12"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        """,
    )
    with pytest.raises(SchemaError, match="differ only in"):
        validate_graph(parse_all(tmp_path))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        pytest.param(
            """
            [matrix.build]
            execution = "hpc"
            triggers = ["rebuild-request"]
            job-script = "./.ci/hpc/build.sh"
            action = "./.github/actions/build-a"
            needs = []

            [[matrix.build.include]]
            runs-on = "hpc-login-selfhosted"
            site = "hpc-batch"
            platform = "hpc-atos-gnu"
            """,
            "execution = 'hpc' and `action`",
            id="hpc-kind-with-action",
        ),
        pytest.param(
            """
            [matrix.build]
            execution = "hpc"
            triggers = ["rebuild-request"]
            needs = []

            [[matrix.build.include]]
            runs-on = "hpc-login-selfhosted"
            site = "hpc-batch"
            platform = "hpc-atos-gnu"
            """,
            "no `job-script`",
            id="hpc-kind-without-job-script",
        ),
        pytest.param(
            """
            [matrix.build]
            triggers = ["rebuild-request"]
            action = "./.github/actions/build-a"
            job-script = "./.ci/hpc/build.sh"
            needs = []

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            build-type = "Release"
            """,
            "`job-script` only applies to execution = 'hpc'",
            id="runner-kind-with-job-script",
        ),
        pytest.param(
            """
            [matrix.build]
            execution = "cloud"
            triggers = ["rebuild-request"]
            job-script = "./.ci/hpc/build.sh"
            needs = []

            [[matrix.build.include]]
            runs-on = "hpc-login-selfhosted"
            site = "hpc-batch"
            platform = "hpc-atos-gnu"
            """,
            None,
            id="unknown-execution",
        ),
    ],
)
def test_schema_rejects(tmp_path: Path, body: str, match: str | None) -> None:
    write_repo(tmp_path, "a", body)
    with pytest.raises(SchemaError, match=match):
        parse_all(tmp_path)


def _hpc_repo(tmp_path: Path, body: str, recipe: str | None = None) -> Path:
    manifest = write_repo(tmp_path, "pkg", body)
    if recipe is not None:
        script = manifest.parents[1] / ".ci" / "hpc" / "build.sh.j2"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(recipe)
    return manifest


_TEMPLATED_MANIFEST = """
    [[matrix.build.include]]
    platform = "hpc-atos-gnu"
    cc = "gcc"
    [[matrix.build.include]]
    platform = "hpc-atos-intel"
    cc = "icx"
    [matrix.build]
    execution = "hpc"
    job-script = "./.ci/hpc/build.sh.j2"
    triggers = ["rebuild-request"]
    needs = []
"""
_BASE_MANIFEST = """
    [[matrix.build.include]]
    platform = "hpc-atos-gnu"
    build-type = "RelWithDebInfo"
    modules = ["load cmake"]
    cc = "gcc"
    cxx = "g++"
    [matrix.build]
    execution = "hpc"
    job-script = "./.ci/hpc/build.sh.j2"
    triggers = ["rebuild-request"]
    needs = []
"""
_EXTENDS = '{% extends "ci-infrastructure/cmake-build.sh.j2" %}\n'


def test_hpc_step_forwards_the_matrix_leg(tmp_path: Path) -> None:
    yaml = render_single(tmp_path, _TEMPLATED_MANIFEST, EXECUTION_HPC, name="pkg")
    assert "matrix-leg: ${{ toJSON(matrix) }}" in yaml


@pytest.mark.parametrize(
    ("body", "recipe"),
    [
        (_TEMPLATED_MANIFEST, "#!/bin/bash\nexport CC={{ cc }}\n"),
        (_BASE_MANIFEST, _EXTENDS),
        (_TEMPLATED_MANIFEST.replace("build.sh.j2", "nowhere.sh"), None),
    ],
    ids=["declared-keys", "extends-base", "plain-sh-never-read"],
)
def test_templated_recipe_validates(tmp_path: Path, body: str, recipe: str | None) -> None:
    validate_job_templates(parse_manifest(_hpc_repo(tmp_path, body, recipe)))


@pytest.mark.parametrize(
    ("body", "recipe", "match"),
    [
        (_TEMPLATED_MANIFEST, "#!/bin/bash\nexport FC={{ fortran }}\n", "fortran"),
        (_TEMPLATED_MANIFEST, None, "does not exist"),
        (_TEMPLATED_MANIFEST, "#!/bin/bash\n{% for x in %}\n", r"build\.sh\.j2:2"),
        (_BASE_MANIFEST, _EXTENDS + "{% block preflight %}{{ boost_root }}{% endblock %}\n", "boost_root"),
        (_TEMPLATED_MANIFEST, _EXTENDS, "modules"),
    ],
    ids=["undeclared-key", "missing", "syntax-error", "child-block-undeclared", "base-key-required"],
)
def test_templated_recipe_rejected(tmp_path: Path, body: str, recipe: str | None, match: str) -> None:
    with pytest.raises(SchemaError, match=match):
        validate_job_templates(parse_manifest(_hpc_repo(tmp_path, body, recipe)))


def test_hpc_job_name_defers_to_the_resolved_slot(tmp_path: Path) -> None:
    yaml = render_single(
        tmp_path,
        """
        [package]
        name = "pkg"
        prefix = "pkg"
        repo = "org/pkg"
        compiler-inputs = ["cxx-compiler"]

        [[matrix.build.include]]
        platform = "hpc-atos-gnu"
        cxx-compiler = "g++-8"
        cc = "gcc"
        modules = ["load prgenv/gnu"]
        [[matrix.build.include]]
        platform = "hpc-atos-intel"
        cxx-compiler = "icpx"
        cc = "icx"
        modules = ["load prgenv/intel-llvm"]
        [matrix.build]
        execution = "hpc"
        job-script = "./.ci/hpc/build.sh.j2"
        triggers = ["rebuild-request"]
        needs = []
        """,
        EXECUTION_HPC,
        name="pkg",
    )
    assert "name: pkg/build (${{ matrix._resolved['job-name'] }})" in yaml
    assert "matrix.cc" not in yaml
    assert "matrix.modules" not in yaml
