# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# (C) Copyright 2026 - ECMWF and individual contributors.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation nor
# does it submit to any jurisdiction.

"""Tests for the HPC (SLURM) execution path in generate_downstream_ci.

An HPC kind is an ordinary matrix kind with ``execution = "hpc"``: it names a
``job-script`` instead of an ``action``, runs on a login-node self-hosted
runner, and its job invokes the shared build-on-hpc action. Artifact identity
and the needs-graph must be identical to the runner path.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ci_infrastructure.generate_downstream_ci import (
    EXECUTION_HPC,
    Manifest,
    SchemaError,
    discover_manifests,
    parse_manifest,
    render_workflow,
)


def write_repo(root: Path, repo_name: str, manifest_body: str) -> Path:
    manifest = root / repo_name / ".ci" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(textwrap.dedent(manifest_body))
    return manifest


def parse_all(root: Path) -> list[Manifest]:
    return [parse_manifest(p) for p in discover_manifests(root)]


_HPC_MANIFEST = """
    [package]
    name = "a"
    repo = "org/a"
    compiler-inputs = []

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


def test_hpc_job_uses_build_on_hpc_action(tmp_path: Path) -> None:
    write_repo(tmp_path, "a", _HPC_MANIFEST)
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    # The HPC build step calls the shared action, not a per-repo composite.
    assert "uses: ecmwf-enterprise-sandbox/ci-infrastructure/actions/build-on-hpc@main" in yaml
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
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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


def test_hpc_step_uses_hpc_ci_ssh_user_secret(tmp_path: Path) -> None:
    """troika-user must come from the HPC_CI_SSH_USER secret the sandbox defines,
    matching every hand-written repo ci.yml (not an undefined TROIKA_USER)."""
    write_repo(tmp_path, "a", _HPC_MANIFEST)
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    assert "troika-user: ${{ secrets.HPC_CI_SSH_USER }}" in yaml
    assert "TROIKA_USER" not in yaml


def test_hpc_fetch_step_stages_python_wheels_without_installing(tmp_path: Path) -> None:
    write_repo(tmp_path, "a", _HPC_MANIFEST)
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    # No setup-python runs on an HPC leg, so there is no consumer interpreter to
    # install needs-python wheels into; fetch-and-publish must be told to stage
    # them instead of failing the preflight that demands --consumer-python.
    assert "install-python-deps: 'false'" in yaml
    assert "actions/setup-python" not in yaml


def test_hpc_job_runs_on_login_runner_without_container(tmp_path: Path) -> None:
    write_repo(tmp_path, "a", _HPC_MANIFEST)
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    assert "runs-on: ${{ matrix['runs-on'] }}" in yaml
    # HPC jobs run in host mode on the login node — no container block.
    assert "container:" not in yaml


def test_hpc_job_has_no_separate_publish_step(tmp_path: Path) -> None:
    """build-on-hpc publishes internally (gated on cache-hit), so the generator
    must not also emit the runner-path publish step."""
    write_repo(tmp_path, "a", _HPC_MANIFEST)
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_HPC)
    assert yaml is not None
    assert "mode: publish" not in yaml


def test_hpc_and_runner_legs_share_artifact_identity(tmp_path: Path) -> None:
    """A runner leg and an HPC leg that differ only in scheduling (runs-on/site)
    but share platform/compiler/build-type collide — proving `site` is treated
    as scheduling, not identity."""
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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
        from ci_infrastructure.generate_downstream_ci import validate_graph

        validate_graph([m])


def test_hpc_leg_accepts_list_runs_on(tmp_path: Path) -> None:
    """runs-on may be a label array (e.g. [self-hosted, linux, hpc]); the render
    path (which builds a set of leg values for the display name) must not choke
    on the unhashable list."""
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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
    assert "uses: ecmwf-enterprise-sandbox/ci-infrastructure/actions/build-on-hpc@main" in yaml


def test_hpc_kind_rejects_action(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
