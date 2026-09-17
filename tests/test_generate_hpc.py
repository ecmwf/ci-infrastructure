# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `execution = "hpc"` kinds in generate_downstream_ci."""

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


def test_hpc_job_uses_build_on_hpc_action(hpc_yaml: str) -> None:
    yaml = hpc_yaml
    assert "uses: ecmwf/ci-infrastructure/actions/build-on-hpc@main" in yaml
    assert "matrix.job-script || './.ci/hpc/build.sh'" in yaml
    assert "site: ${{ matrix.site }}" in yaml
    assert "Fetch resolved deps" in yaml
    assert "actions/fetch-deps@main" in yaml
    assert "cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}" in yaml


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


def test_hpc_step_uses_hpc_ci_ssh_user_secret(hpc_yaml: str) -> None:
    assert "troika-user: ${{ secrets.HPC_CI_SSH_USER }}" in hpc_yaml


def test_hpc_fetch_step_stages_python_wheels_without_installing(hpc_yaml: str) -> None:
    """No setup-python on an HPC leg, so there is no consumer interpreter."""
    assert "install-python-deps: 'false'" in hpc_yaml
    assert "actions/setup-python" not in hpc_yaml


def test_hpc_job_threads_the_leg_container(hpc_yaml: str) -> None:
    """The cluster ssh identity lives in the image; an empty image falls back to host mode."""
    assert "runs-on: ${{ matrix['runs-on'] }}" in hpc_yaml
    assert "container:" in hpc_yaml
    assert "image: ${{ matrix.container || '' }}" in hpc_yaml


def test_container_credentials_are_opt_in(tmp_path: Path) -> None:
    assert "credentials:" not in render_single(tmp_path / "plain", _HPC_MANIFEST, EXECUTION_HPC)

    yaml = render_single(
        tmp_path / "creds",
        _HPC_MANIFEST.replace('execution = "hpc"', 'execution = "hpc"\ncontainer-credentials = true'),
        EXECUTION_HPC,
    )
    assert "credentials:" in yaml
    assert "username: ${{ secrets.ECCR_PULL_ROBOT_NAME }}" in yaml
    assert "password: ${{ secrets.ECCR_PULL_ROBOT_TOKEN }}" in yaml


def test_hpc_job_has_no_separate_publish_step(hpc_yaml: str) -> None:
    """build-on-hpc publishes internally."""
    assert "actions/publish-artifact@main" not in hpc_yaml


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


def test_hpc_leg_accepts_list_runs_on(tmp_path: Path) -> None:
    yaml = render_single(
        tmp_path,
        """
        [matrix.build-hpc]
        execution = "hpc"
        triggers = ["rebuild-request"]
        job-script = "./.ci/hpc/build.sh"
        needs = []

        [[matrix.build-hpc.include]]
        runs-on = ["self-hosted", "linux", "hpc"]
        site = "hpc-batch"
        compiler = "gnu-12"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        """,
        EXECUTION_HPC,
    )
    assert "uses: ecmwf/ci-infrastructure/actions/build-on-hpc@main" in yaml


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


# === Templated recipes (.j2) ===============================================


def _hpc_repo(tmp_path: Path, body: str, recipe: str | None = None, name: str = "build.sh.j2") -> Path:
    manifest = write_repo(tmp_path, "pkg", body)
    if recipe is not None:
        script = manifest.parents[1] / ".ci" / "hpc" / name
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


def test_hpc_step_forwards_the_matrix_leg(tmp_path: Path) -> None:
    yaml = render_single(tmp_path, _TEMPLATED_MANIFEST, EXECUTION_HPC, name="pkg")
    assert "matrix-leg: ${{ toJSON(matrix) }}" in yaml


def test_templated_recipe_reading_only_declared_keys_validates(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _TEMPLATED_MANIFEST, "#!/bin/bash\nexport CC={{ cc }}\n")
    validate_job_templates(parse_manifest(m))


def test_templated_recipe_reading_an_undeclared_key_is_rejected(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _TEMPLATED_MANIFEST, "#!/bin/bash\nexport FC={{ fortran }}\n")
    with pytest.raises(SchemaError, match="fortran"):
        validate_job_templates(parse_manifest(m))


def test_missing_templated_recipe_is_rejected(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _TEMPLATED_MANIFEST)
    with pytest.raises(SchemaError, match="does not exist"):
        validate_job_templates(parse_manifest(m))


def test_templated_recipe_syntax_error_names_its_line(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _TEMPLATED_MANIFEST, "#!/bin/bash\n{% for x in %}\n")
    with pytest.raises(SchemaError, match=r"build\.sh\.j2:2"):
        validate_job_templates(parse_manifest(m))


def test_a_plain_sh_job_script_is_never_read_from_disk(tmp_path: Path) -> None:
    m = write_repo(
        tmp_path,
        "pkg",
        """
        [[matrix.build.include]]
        platform = "hpc-atos-gnu"
        [matrix.build]
        execution = "hpc"
        job-script = "./.ci/hpc/nowhere.sh"
        triggers = ["rebuild-request"]
        needs = []
        """,
    )
    validate_job_templates(parse_manifest(m))


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


def test_recipe_extending_the_base_validates_on_defaults(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _BASE_MANIFEST, _EXTENDS)
    validate_job_templates(parse_manifest(m))


def test_child_block_reading_an_undeclared_key_is_rejected(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _BASE_MANIFEST, _EXTENDS + "{% block preflight %}{{ boost_root }}{% endblock %}\n")
    with pytest.raises(SchemaError, match="boost_root"):
        validate_job_templates(parse_manifest(m))


def test_key_the_base_reads_is_still_required_of_the_leg(tmp_path: Path) -> None:
    m = _hpc_repo(tmp_path, _TEMPLATED_MANIFEST, _EXTENDS)
    with pytest.raises(SchemaError, match="modules"):
        validate_job_templates(parse_manifest(m))


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
