# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the HPC (SLURM) execution path in generate_downstream_ci.

An HPC kind is an ordinary matrix kind with ``execution = "hpc"``: it names a
``job-script`` instead of an ``action``, runs on a login-node self-hosted
runner, and its job invokes the shared build-on-hpc action. Artifact identity
and the needs-graph must be identical to the runner path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import parse_all, write_repo

from ci_infrastructure.generate_downstream_ci import (
    EXECUTION_HPC,
    SchemaError,
    render_workflow,
    validate_graph,
)

_HPC_MANIFEST = """
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
    platform = "atos-hpc-gnu"
    """


@pytest.fixture
def hpc_yaml(tmp_path: Path) -> str:
    """cross-repo-trigger-hpc.yml for a single one-leg HPC repo.

    Five assertions below are about different parts of the same rendered file,
    so it is rendered once rather than rebuilt per test.
    """
    write_repo(tmp_path, "a", _HPC_MANIFEST)
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None, "the hpc lane must render when the only kind is hpc"
    return yaml


def test_hpc_job_uses_build_on_hpc_action(hpc_yaml: str) -> None:
    yaml = hpc_yaml
    # The HPC build step calls the shared action, not a per-repo composite.
    assert "uses: ecmwf/ci-infrastructure/actions/build-on-hpc@main" in yaml
    # A leg with no per-leg job-script falls back to the kind-level default.
    assert "matrix.job-script || './.ci/hpc/build.sh'" in yaml
    assert "site: ${{ matrix.site }}" in yaml
    # Deps still flow through the shared fetch step exactly like the runner path.
    assert "Fetch resolved deps" in yaml
    assert "mode: download-only" in yaml
    assert "cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}" in yaml


def test_hpc_job_script_is_per_leg_with_kind_level_fallback(tmp_path: Path) -> None:
    """A multi-recipe HPC kind names job-script per leg (e.g. one per Python
    version). The step must forward the per-leg value (``matrix.job-script``),
    not the kind-level default baked in for every leg — otherwise every leg runs
    the same recipe regardless of which one the matrix selected."""
    write_repo(
        tmp_path,
        "a",
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
        platform = "atos-hpc-gnu"
        job-script = "./.ci/hpc/build-py3.11.sh"

        [[matrix.build-hpc.include]]
        runs-on = ["self-hosted", "linux", "hpc"]
        site = "hpc-batch"
        python-version = "3.12"
        build-type = "Release"
        platform = "atos-hpc-gnu"
        job-script = "./.ci/hpc/build-py3.12.sh"
        """,
    )
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    assert "matrix.job-script || './.ci/hpc/build-py3.12.sh'" in yaml
    # The kind-level default must not be hardcoded as a bare job-script value.
    assert "job-script: ./.ci/hpc/build-py3.12.sh" not in yaml


def test_hpc_test_only_kind_passes_publish_false(tmp_path: Path) -> None:
    """A non-publishing HPC kind (publishes = false) must run build-on-hpc with
    publish: false, so the action skips the fetch of an install tree the pure
    test never creates (the reported build-hpc failure)."""
    write_repo(
        tmp_path,
        "a",
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
        platform = "atos-hpc-gnu"
        job-script = "./.ci/hpc/test.sh"
        """,
    )
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    assert "publish: 'false'" in yaml
    # A publishing kind would still fetch + publish; a test kind names the step "Run".
    assert "name: Run on HPC" in yaml


def test_hpc_step_uses_hpc_ci_ssh_user_secret(hpc_yaml: str) -> None:
    """troika-user must come from the HPC_CI_SSH_USER secret the sandbox defines,
    matching every hand-written repo ci.yml (not an undefined TROIKA_USER)."""
    yaml = hpc_yaml
    assert "troika-user: ${{ secrets.HPC_CI_SSH_USER }}" in yaml
    assert "TROIKA_USER" not in yaml


def test_hpc_fetch_step_stages_python_wheels_without_installing(hpc_yaml: str) -> None:
    yaml = hpc_yaml
    # No setup-python runs on an HPC leg, so there is no consumer interpreter to
    # install needs-python wheels into; fetch-and-publish must be told to stage
    # them instead of failing the preflight that demands --consumer-python.
    assert "install-python-deps: 'false'" in yaml
    assert "actions/setup-python" not in yaml


def test_hpc_job_runs_on_login_runner_without_container(hpc_yaml: str) -> None:
    yaml = hpc_yaml
    assert "runs-on: ${{ matrix['runs-on'] }}" in yaml
    # HPC jobs run in host mode on the login node — no container block.
    assert "container:" not in yaml


def test_hpc_job_has_no_separate_publish_step(hpc_yaml: str) -> None:
    """build-on-hpc publishes internally (gated on cache-hit), so the generator
    must not also emit the runner-path publish step."""
    yaml = hpc_yaml
    assert "mode: publish" not in yaml


def test_legs_differing_only_by_site_collide(tmp_path: Path) -> None:
    """Two legs that differ only in `site` share one artifact identity, proving
    `site` is scheduling and not part of the name — so they would publish two
    different builds under a single name."""
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
        platform = "atos-hpc-gnu"

        [[matrix.build.include]]
        runs-on = "hpc-login-selfhosted"
        site = "lumi"
        compiler = "gnu-12"
        build-type = "Release"
        platform = "atos-hpc-gnu"
        """,
    )
    [m] = parse_all(tmp_path)
    with pytest.raises(SchemaError, match="differ only in"):
        validate_graph([m])


def test_hpc_leg_accepts_list_runs_on(tmp_path: Path) -> None:
    """runs-on may be a label array (e.g. [self-hosted, linux, hpc]); the render
    path (which builds a set of leg values for the display name) must not choke
    on the unhashable list."""
    write_repo(
        tmp_path,
        "a",
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
        platform = "atos-hpc-gnu"
        """,
    )
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    assert "uses: ecmwf/ci-infrastructure/actions/build-on-hpc@main" in yaml


def test_hpc_kind_rejects_action(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
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
        platform = "atos-hpc-gnu"
        """,
    )
    with pytest.raises(SchemaError, match="execution = 'hpc' and `action`"):
        parse_all(tmp_path)


def test_hpc_kind_requires_job_script_when_triggered(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        execution = "hpc"
        triggers = ["rebuild-request"]
        needs = []

        [[matrix.build.include]]
        runs-on = "hpc-login-selfhosted"
        site = "hpc-batch"
        platform = "atos-hpc-gnu"
        """,
    )
    with pytest.raises(SchemaError, match="no `job-script`"):
        parse_all(tmp_path)


def test_runner_kind_rejects_job_script(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
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
    )
    with pytest.raises(SchemaError, match="`job-script` only applies to execution = 'hpc'"):
        parse_all(tmp_path)


def test_unknown_execution_value_rejected(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        execution = "cloud"
        triggers = ["rebuild-request"]
        job-script = "./.ci/hpc/build.sh"
        needs = []

        [[matrix.build.include]]
        runs-on = "hpc-login-selfhosted"
        site = "hpc-batch"
        platform = "atos-hpc-gnu"
        """,
    )
    with pytest.raises(SchemaError):
        parse_all(tmp_path)
