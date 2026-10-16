# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""`fork-pull-requests`: the opt-in, ci-fork.yml, and how downstream CI and the resolver treat a fork."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from conftest import parse_all, write_repo

from ci_infrastructure import resolve_deps
from ci_infrastructure._github_api import EXECUTION_RUNNER, ManifestSchemaError
from ci_infrastructure.generate_downstream_ci import (
    FORK_CI_NAME,
    FORK_CI_WORKFLOW,
    _run,
    compute_transitive_consumers,
    render_fork_workflow,
    render_orchestrator_workflow,
    validate_graph,
)
from ci_infrastructure.manifest import validate

_PACKAGE: Final = """
    [package]
    name = "a"
    repo = "org/a"
    compiler-inputs = []
    visibility = "public"
    fork-pull-requests = {opt_in}
"""

_UPSTREAM: Final = """
    [[trigger-downstream]]
    repo = "org/b"
    ref = "main"
    [matrix.build]
    triggers = ["upstream-change"]
    defaults.job-script = "./.ci/build.sh"
    needs = []
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
"""

_CONSUMER: Final = """
    [[deps]]
    repo = "org/a"
    package = "a"
    ref = "main"
    compiler-inputs = []
    [matrix.build]
    triggers = ["upstream-change"]
    defaults.job-script = "./.ci/build.sh"
    needs = ["a/build"]
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
"""


def _repos(tmp_path: Path, *, opt_in: bool) -> None:
    write_repo(tmp_path, "a", _PACKAGE.format(opt_in=str(opt_in).lower()) + _UPSTREAM)
    write_repo(tmp_path, "b", _CONSUMER)


def _orch(tmp_path: Path) -> dict[Any, Any]:
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    out = render_orchestrator_workflow(
        by_pkg["a"],
        by_pkg,
        {m.repo: m for m in manifests},
        compute_transitive_consumers(manifests),
        lane=EXECUTION_RUNNER,
    )
    assert out is not None
    doc: dict[Any, Any] = yaml.safe_load(out)
    return doc


def _package(**extra: Any) -> dict[str, Any]:
    return {"package": {"name": "a", "repo": "org/a", "compiler-inputs": [], **extra}}


def test_fork_pull_requests_is_off_by_default() -> None:
    assert validate(_package()).package.fork_pull_requests is False


def test_fork_pull_requests_needs_a_public_repo() -> None:
    with pytest.raises(ManifestSchemaError, match="fork-pull-requests needs visibility = 'public'"):
        validate(_package(**{"fork-pull-requests": True}))
    assert validate(_package(**{"fork-pull-requests": True, "visibility": "public"})).package.fork_pull_requests


def test_without_the_opt_in_there_is_no_fork_workflow(tmp_path: Path) -> None:
    _repos(tmp_path, opt_in=False)
    [a, _] = parse_all(tmp_path)
    assert render_fork_workflow(a) is None


def test_the_fork_workflow_calls_ci_yml_on_an_approval_or_a_push(tmp_path: Path) -> None:
    _repos(tmp_path, opt_in=True)
    [a, _] = parse_all(tmp_path)
    rendered = render_fork_workflow(a)
    assert rendered is not None
    doc: dict[Any, Any] = yaml.safe_load(rendered)

    assert doc["name"] == FORK_CI_NAME
    # resolve-dispatch-context reads the PR and its commit back from here.
    assert doc["run-name"].endswith(
        "#${{ github.event.pull_request.number }} @ ${{ github.event.pull_request.head.sha }}"
    )
    assert doc[True] == {"pull_request_target": {"types": ["labeled", "synchronize", "reopened"]}}
    assert doc["permissions"] == {"contents": "read", "pull-requests": "write"}
    (job,) = doc["jobs"].values()
    assert job["uses"] == "./.github/workflows/ci.yml"
    assert job["secrets"] == "inherit"
    assert "github.event.pull_request.head.repo.full_name != github.repository" in job["if"]
    assert "github.event.label.name == 'approved-for-ci'" in job["if"]


def test_the_generator_writes_the_fork_workflow_and_removes_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repos(tmp_path, opt_in=True)
    monkeypatch.chdir(tmp_path / "a")
    fork_wf = tmp_path / "a" / ".github" / "workflows" / FORK_CI_WORKFLOW

    _run(".ci/manifest.toml", check=False, sibling_root=tmp_path)
    assert fork_wf.is_file()

    manifest = tmp_path / "a" / ".ci" / "manifest.toml"
    manifest.write_text(manifest.read_text().replace("fork-pull-requests = true", "fork-pull-requests = false"))
    _run(".ci/manifest.toml", check=False, sibling_root=tmp_path)
    assert not fork_wf.exists()


def test_downstream_listens_to_the_fork_ci_only_with_the_opt_in(tmp_path: Path) -> None:
    _repos(tmp_path, opt_in=False)
    plain = _orch(tmp_path)
    assert plain[True]["workflow_run"]["workflows"] == ["CI"]
    assert "fork-ci-workflow" not in plain["jobs"]["context"]["steps"][0]["with"]

    _repos(tmp_path, opt_in=True)
    forked = _orch(tmp_path)
    assert forked[True]["workflow_run"]["workflows"] == ["CI", FORK_CI_NAME]
    assert forked["jobs"]["context"]["steps"][0]["with"]["fork-ci-workflow"] == FORK_CI_WORKFLOW
    # A fork's run carries the base branch's commit: its run-name keeps two PRs' runs apart.
    assert (
        f"github.event.workflow_run.name == '{FORK_CI_NAME}' && github.event.workflow_run.display_title"
        in (forked["concurrency"]["group"])
    )


def test_downstream_can_be_dispatched_for_a_pull_request(tmp_path: Path) -> None:
    _repos(tmp_path, opt_in=False)
    doc = _orch(tmp_path)
    assert sorted(doc[True]["workflow_dispatch"]["inputs"]) == ["level", "pull-request"]
    assert doc["jobs"]["context"]["steps"][0]["with"]["pull-request"] == "${{ inputs.pull-request }}"
    gate = doc["jobs"]["label-gate"]["steps"][0]["with"]
    assert gate["level"] == "${{ inputs.level }}"
    assert gate["pull-request"] == "${{ needs.context.outputs.pull-request }}"


def test_downstream_has_no_pull_request_trigger(tmp_path: Path) -> None:
    """A fork's label event had no secrets anyway; the label gate dispatches, in the base context."""
    _repos(tmp_path, opt_in=True)
    doc = _orch(tmp_path)
    assert "pull_request" not in doc[True]
    assert "if" not in doc["jobs"]["context"]


_RESOLVE_MANIFEST: Final = textwrap.dedent("""
    [package]
    name = "a"
    repo = "org/a"
    compiler-inputs = []
    [matrix.build]
    defaults.job-script = "./.ci/build.sh"
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
    platform = "ubuntu-24.04"
""")


def test_a_pin_of_this_repo_is_its_own_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fork's branch name means nothing here: the pin, not the branch, names the commit."""
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(_RESOLVE_MANIFEST)
    outputs: dict[str, str] = {}
    monkeypatch.setattr(resolve_deps, "write_outputs", outputs.update)
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: pytest.fail("looked up a ref"))

    resolve_deps._run(str(manifest), "develop", "build", "org/a", ".ci/manifest.toml", pin=f"org/a@{'f' * 40}")

    [leg] = yaml.safe_load(outputs["json"])["build"]["include"]
    assert leg["_resolved"]["own-sha"] == "f" * 40
    assert "f" * 40 in leg["_resolved"]["own-artifact-name"]


def test_the_generated_fork_workflow_passes_check_ci_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_check_ci_approval import FORK_CI_YML

    from ci_infrastructure import check_ci_approval

    _repos(tmp_path, opt_in=True)
    workflows = tmp_path / "a" / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(textwrap.dedent(FORK_CI_YML))
    monkeypatch.chdir(tmp_path / "a")
    _run(".ci/manifest.toml", check=False, sibling_root=tmp_path)

    assert check_ci_approval.check(workflows / FORK_CI_WORKFLOW) == []
    assert check_ci_approval.check(workflows / "ci.yml") == []
