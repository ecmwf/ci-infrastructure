# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for generate_downstream_ci: schema checks, graph validation and rendered workflows."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from conftest import parse_all, render_single, write_repo

from ci_infrastructure._errors import CIError
from ci_infrastructure._github_api import EXECUTION_HPC, EXECUTION_RUNNER, Execution
from ci_infrastructure.generate_downstream_ci import (
    ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS,
    ORCHESTRATOR_MAX_TOTAL_JOBS,
    SLIM_RUNNER,
    SchemaError,
    _cross_package_deps,
    _fetch_sibling_manifests,
    _local_sibling_layer,
    _run,
    _write_or_check_path,
    compute_transitive_consumers,
    parse_manifest_text,
    render_orchestrator_workflow,
    render_workflow,
    resolve_consumer_refs,
    transitive_cross_repo_needs,
    validate_graph,
)


def _render_orch(tmp_path: Path, lane: Execution = EXECUTION_RUNNER, pkg: str = "a") -> str | None:
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    closures = compute_transitive_consumers(manifests)
    return render_orchestrator_workflow(by_pkg[pkg], by_pkg, by_repo, closures, lane=lane)


def _orch(tmp_path: Path, lane: Execution = EXECUTION_RUNNER) -> str:
    out = _render_orch(tmp_path, lane)
    assert out is not None
    return out


def _consumer(tmp_path: Path, pkg: str, lane: Execution = EXECUTION_RUNNER) -> str:
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    out = render_workflow(by_pkg[pkg], by_pkg, lane=lane)
    assert out is not None
    return out


_CTEST_MANIFEST: Final = """
    [matrix.build]
    triggers = ["upstream-change", "rebuild-request"]
    action = "./.github/actions/build-thisrepo"
    needs = []
    {extra}

    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
    build-type = "Release"
    """


@pytest.mark.parametrize(
    ("body", "match"),
    [
        pytest.param(
            """
            [matrix.build]
            triggers = ["upstream-change", "rebuild-request"]
            action = "./.github/actions/build"
            bogus = "x"

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
            "unknown key",
            id="unknown-matrix-key",
        ),
        pytest.param(
            """
            [matrix.build]
            triggers = ["rebuild-request"]
            needs = []

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
            "has no `action`",
            id="triggered-kind-without-action",
        ),
        pytest.param(
            """
            [matrix.build]
            triggers = ["rebuild-request"]
            action = "../etc/passwd"
            needs = []

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
            "local composite path",
            id="action-not-local-composite",
        ),
        pytest.param(
            """
            [matrix.build]
            triggers = ["rebuild-request"]
            action = "./.github/actions/build-a"
            forwarded-inputs = ["typoed-field"]
            needs = []

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            build-type = "Release"
            """,
            "typoed-field",
            id="forwarded-input-typo",
        ),
        pytest.param(
            """
            [matrix.build]
            artifact-prefix = ""
            triggers = ["rebuild-request"]
            action = "./.github/actions/build-a"
            needs = []

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
            "artifact-prefix must be a non-empty string",
            id="empty-artifact-prefix",
        ),
        pytest.param(
            _CTEST_MANIFEST.format(extra='ctest-args = "-E slow"'),
            "ctest-args.*without",
            id="ctest-args-without-ctest",
        ),
        pytest.param(
            """
            [matrix.build-hpc]
            execution = "hpc"
            triggers = ["rebuild-request"]
            job-script = "./.ci/hpc/build.sh"
            ctest = true
            needs = []

            [[matrix.build-hpc.include]]
            runs-on = "hpc-login-selfhosted"
            site = "hpc-batch"
            build-type = "Release"
            platform = "hpc-atos-gnu"
            """,
            "ctest.*execution = 'hpc'",
            id="ctest-on-hpc-kind",
        ),
        pytest.param(
            """
            [matrix.build]
            triggers = ["upstream-change"]
            action = "./.github/actions/run-checks"
            publishes = false
            ctest = true
            needs = []

            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            build-type = "Release"
            """,
            "ctest.*publishes = false",
            id="ctest-on-non-publishing-kind",
        ),
        pytest.param(
            """
            [[trigger-downstream]]
            repo = "org/b"
            extra = "nope"
            """,
            "must define exactly 'repo' and 'ref'",
            id="trigger-downstream-unknown-key",
        ),
        pytest.param(
            """
            [[trigger-downstream]]
            repo = "org/b"
            """,
            "must define exactly 'repo' and 'ref'",
            id="trigger-downstream-without-ref",
        ),
        pytest.param(
            """
            [[trigger-downstream]]
            repo = "org/b"
            ref = "main"

            [[trigger-downstream]]
            repo = "org/b"
            ref = "main"
            """,
            "duplicate",
            id="duplicate-trigger-downstream",
        ),
        pytest.param(
            """
            [matrix.test]
            reuse-matrix = "build"
            triggers = ["upstream-change", "rebuild-request"]
            action = "./.github/actions/build"
            needs = ["build"]
            """,
            "reuse-matrix",
            id="reuse-matrix-target-missing",
        ),
        pytest.param(
            """
            [package]
            name = "a"
            repo = "org/a"
            visibility = "secret"
            compiler-inputs = []
            [matrix.build]
            triggers = ["rebuild-request"]
            action = "./.github/actions/build"
            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
            "visibility",
            id="invalid-visibility",
        ),
        pytest.param(
            """
            [package]
            name = "a"
            repo = "org/a"
            submodules = "yes"
            compiler-inputs = []
            [matrix.build]
            triggers = ["rebuild-request"]
            action = "./.github/actions/build"
            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
            "submodules",
            id="invalid-submodules",
        ),
        pytest.param(
            """
            [generated]
            header = "name: not-a-comment"
            """,
            r"\[generated\].header.*must be YAML comments",
            id="header-not-comments",
        ),
    ],
)
def test_schema_rejects(tmp_path: Path, body: str, match: str) -> None:
    write_repo(tmp_path, "a", body)
    with pytest.raises(SchemaError, match=match):
        parse_all(tmp_path)


def test_artifact_prefix_is_an_accepted_kind_key(tmp_path: Path) -> None:
    """resolve_deps applies the value; the generator only accepts the key."""
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        prefix = "a-primary"
        repo = "org/a"
        compiler-inputs = []

        [matrix.build]
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a"
        needs = []

        [matrix.build-secondary]
        artifact-prefix = "a-secondary"
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a-secondary"
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"

        [[matrix.build-secondary.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    [m] = parse_all(tmp_path)
    assert set(m.matrices) == {"build", "build-secondary"}


def test_setup_python_emitted_when_leg_has_python_version(tmp_path: Path) -> None:
    """fetch_deps' pip install must run on the leg's interpreter."""
    yaml = render_single(
        tmp_path,
        """
        [matrix.test]
        triggers = ["upstream-change"]
        action = "./.github/actions/test-a"
        forwarded-inputs = ["python-version"]
        publishes = false
        needs = []

        [[matrix.test.include]]
        runs-on = "ubuntu-latest"
        python-version = "3.10"
        """,
    )
    assert "uses: actions/setup-python@v6" in yaml
    assert "python-version: ${{ steps.m.outputs.python-version }}" in yaml
    assert yaml.index("Decode matrix-leg") < yaml.index("Set up Python") < yaml.index("Fetch resolved deps")


def test_setup_python_omitted_when_no_leg_has_python_version(tmp_path: Path) -> None:
    """An empty `python-version:` input would fail actions/setup-python."""
    yaml = render_single(
        tmp_path,
        """
        [package]
        name = "a"
        prefix = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.build]
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a"
        forwarded-inputs = ["cxx-compiler"]
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        cxx-compiler = "clang++-18"
        """,
    )
    assert "actions/setup-python" not in yaml
    assert "Set up Python" not in yaml


def test_job_name_defers_to_the_resolved_slot(tmp_path: Path) -> None:
    """The title comes from `_resolved.job-name` (see test_job_names)."""
    yaml = render_single(
        tmp_path,
        """
        [package]
        name = "a"
        prefix = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.build]
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a"
        forwarded-inputs = ["cxx-compiler"]
        needs = []

        [[matrix.build.include]]
        cxx-compiler = "clang++-18"
        platform = "ubuntu-24.04"
        runs-on = "ubuntu-latest"

        [[matrix.build.include]]
        cxx-compiler = "g++-13"
        platform = "ubuntu-24.04"
        runs-on = "ubuntu-latest"
        """,
    )
    # The job and both check-run steps carry the same title.
    assert yaml.count("name: a/build (${{ matrix._resolved['job-name'] }})") == 3


def test_workflow_inlines_build_action(tmp_path: Path) -> None:
    yaml = render_single(
        tmp_path,
        """
        [matrix.build]
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-thisrepo"
        forwarded-inputs = ["build-type"]
        forwarded-deps-outputs = ["cmake-prefix-path"]
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        build-type = "Release"
        """,
    )
    assert "uses: ./.github/actions/build-thisrepo" in yaml
    assert "Decode matrix-leg" in yaml
    assert "command -v jq" in yaml
    assert "Fetch resolved deps" in yaml
    assert "actions/fetch-deps@main" in yaml
    assert "actions/publish-artifact@main" in yaml
    assert "cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}" in yaml
    assert "build-type: ${{ steps.m.outputs.build-type }}" in yaml


def test_ctest_absent_by_default(tmp_path: Path) -> None:
    assert "ctest" not in render_single(tmp_path, _CTEST_MANIFEST.format(extra=""))


def test_ctest_step_emitted_before_publish(tmp_path: Path) -> None:
    """A failing test ends the job before publish, so no red build reaches the store."""
    out = render_single(tmp_path, _CTEST_MANIFEST.format(extra="ctest = true"))
    assert 'ctest --test-dir "${{ steps.build.outputs.build-dir }}" --output-on-failure' in out
    assert out.index("ctest --test-dir") < out.index("actions/publish-artifact@main")


def test_ctest_args_appended_verbatim(tmp_path: Path) -> None:
    out = render_single(tmp_path, _CTEST_MANIFEST.format(extra='ctest = true\n    ctest-args = "-L nightly -E s_http"'))
    assert 'ctest --test-dir "${{ steps.build.outputs.build-dir }}" --output-on-failure -L nightly -E s_http' in out


def test_trigger_downstream_uses_explicit_ref(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "develop"
        """,
    )
    [m] = parse_all(tmp_path)
    assert len(m.triggers) == 1
    assert m.triggers[0].ref == "develop"


def test_parse_manifest_text_round_trip() -> None:
    body = textwrap.dedent(
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []

        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"

        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """
    )
    fake_path = Path("github://org/a@HEAD/.ci/manifest.toml")
    m = parse_manifest_text(body, fake_path)
    assert m.path == fake_path
    assert m.package_name == "a"
    assert m.repo == "org/a"
    assert [t.repo for t in m.triggers] == ["org/b"]
    assert "build" in m.matrices

    with pytest.raises(SchemaError, match="must define exactly 'repo' and 'ref'"):
        parse_manifest_text(
            textwrap.dedent(
                """
                [package]
                name = "a"
                repo = "org/a"
                compiler-inputs = []
                [[trigger-downstream]]
                repo = "org/b"
                ref = "main"
                bogus = "x"
                """
            ),
            fake_path,
        )


def _make_two_repo_pair(tmp_path: Path, *, with_dep_back: bool) -> None:
    """A triggers B; B optionally depends on A."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"

        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    deps_block = (
        """
        [[deps]]
        repo = "org/a"
        package = "a"
        """
        if with_dep_back
        else ""
    )
    write_repo(
        tmp_path,
        "b",
        f"""
        [package]
        name = "b"
        repo = "org/b"
        compiler-inputs = []
        {deps_block}
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build"]

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )


def test_subset_invariant_violated(tmp_path: Path) -> None:
    _make_two_repo_pair(tmp_path, with_dep_back=False)
    with pytest.raises(SchemaError, match="does not list .* as a \\[\\[deps\\]\\]"):
        validate_graph(parse_all(tmp_path))


def test_happy_two_repo_pair(tmp_path: Path) -> None:
    _make_two_repo_pair(tmp_path, with_dep_back=True)
    validate_graph(parse_all(tmp_path))


def test_trigger_cycle(tmp_path: Path) -> None:
    for name, other in (("a", "b"), ("b", "a")):
        write_repo(
            tmp_path,
            name,
            f"""
            [package]
            name = "{name}"
            prefix = "{name}"
            repo = "org/{name}"
            compiler-inputs = []

            [[deps]]
            repo = "org/{other}"
            package = "{other}"

            [[trigger-downstream]]
            repo = "org/{other}"
            ref = "main"

            [matrix.build]
            triggers = ["upstream-change", "rebuild-request"]
            action = "./.github/actions/build"
            needs = []
            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
        )
    with pytest.raises(SchemaError, match="cycle"):
        validate_graph(parse_all(tmp_path))


@pytest.mark.parametrize(
    ("need", "match"),
    [("nope", "local kind 'nope'"), ("ghost/build", "unknown package 'ghost'")],
)
def test_dangling_need(tmp_path: Path, need: str, match: str) -> None:
    write_repo(
        tmp_path,
        "a",
        f"""
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["{need}"]

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match=match):
        validate_graph(parse_all(tmp_path))


def test_cross_repo_need_target_not_runnable(tmp_path: Path) -> None:
    """B needs a/internal, which has no 'upstream-change' trigger."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"

        [matrix.internal]
        needs = []
        [[matrix.internal.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"

        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/internal"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="has no triggers"):
        validate_graph(parse_all(tmp_path))


def test_reachability_violation(tmp_path: Path) -> None:
    """B needs a/build but A doesn't trigger B."""
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"

        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="orchestrator will never call us"):
        validate_graph(parse_all(tmp_path))


def _make_chain_abc(tmp_path: Path) -> None:
    """A -> B -> C (B pins C to develop), one runnable build kind each."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"
        [[trigger-downstream]]
        repo = "org/c"
        ref = "develop"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    write_repo(
        tmp_path,
        "c",
        """
        [[deps]]
        repo = "org/b"
        package = "b"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["b/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )


def test_transitive_cross_repo_needs_recurses(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    refs = transitive_cross_repo_needs(by_pkg["c"], "build", by_pkg)
    assert {(r.package, r.kind) for r in refs} == {("a", "build"), ("b", "build")}


def test_kind_filter_accepts_transitive_originator(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    yaml = _consumer(tmp_path, "c")
    assert "contains(fromJSON(inputs.from-jobs), 'a/build')" in yaml
    assert "contains(fromJSON(inputs.from-jobs), 'b/build')" in yaml
    assert "inputs.rebuild-request" in yaml


def test_chain_closure(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    a_entry = closures["org/a"]["a/build"]
    assert a_entry["consumers"] == ["org/b", "org/c"]
    assert a_entry["expected-checks"] == ["b/build", "c/build"]
    b_entry = closures["org/b"]["b/build"]
    assert b_entry["consumers"] == ["org/c"]
    assert b_entry["expected-checks"] == ["c/build"]
    assert closures["org/c"] == {}


def test_diamond_closure(tmp_path: Path) -> None:
    """B -> C, B -> D, C -> E, D -> E: closure for `b/build` = {C, D, E}, no dupes."""
    write_repo(
        tmp_path,
        "b",
        """
        [[trigger-downstream]]
        repo = "org/c"
        ref = "main"
        [[trigger-downstream]]
        repo = "org/d"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    for name in ("c", "d"):
        write_repo(
            tmp_path,
            name,
            f"""
            [package]
            name = "{name}"
            repo = "org/{name}"
            compiler-inputs = []
            [[deps]]
            repo = "org/b"
            package = "b"
            [[trigger-downstream]]
            repo = "org/e"
            ref = "main"
            [matrix.build]
            triggers = ["upstream-change", "rebuild-request"]
            action = "./.github/actions/build"
            needs = ["b/build"]
            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
        )
    write_repo(
        tmp_path,
        "e",
        """
        [[deps]]
        repo = "org/c"
        package = "c"
        [[deps]]
        repo = "org/d"
        package = "d"
        [matrix.test]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["c/build", "d/build"]
        [[matrix.test.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    b_entry = compute_transitive_consumers(manifests)["org/b"]["b/build"]
    assert b_entry["consumers"] == ["org/c", "org/d", "org/e"]
    assert b_entry["expected-checks"] == ["c/build", "d/build", "e/test"]


def test_external_trigger_pruned(tmp_path: Path) -> None:
    """Triggers pointing at an out-of-scope repo are dropped from the closure."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        [[trigger-downstream]]
        repo = "external-org/external-repo"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    entry = compute_transitive_consumers(manifests)["org/a"]["a/build"]
    assert entry["consumers"] == ["org/b"]
    assert entry["expected-checks"] == ["b/build"]


def test_workflow_dispatch_trigger(tmp_path: Path) -> None:
    """workflow_call and workflow_dispatch share typed inputs; dispatch-id is dispatch-only."""
    _make_chain_abc(tmp_path)
    yaml = _consumer(tmp_path, "c")
    assert "  workflow_call:\n" in yaml
    assert "  workflow_dispatch:\n" in yaml
    for name in ("dispatch-id", "from-repo", "from-sha", "from-jobs", "rebuild-request", "branch", "fallback-ref"):
        assert f"{name}:\n" in yaml
    assert "default: ''" in yaml
    assert "run-name:" in yaml
    assert "${{ inputs.dispatch-id }}" in yaml
    assert "contains(fromJSON(inputs.from-jobs)," in yaml
    assert "inputs.rebuild-request" in yaml
    # A static package token: under workflow_call github.workflow is the caller's.
    assert "group: cross-repo-trigger-runner-c-${{ github.ref }}" in yaml
    assert "cancel-in-progress: false" in yaml


def test_workflow_mints_app_token_per_job(tmp_path: Path) -> None:
    """Masked outputs are redacted across needs, so each job mints its own token."""
    _make_chain_abc(tmp_path)
    yaml = _consumer(tmp_path, "c")
    assert yaml.count("actions/create-github-app-token@v3") >= 2
    assert "${{ steps.mint.outputs.token }}" in yaml
    assert "needs.resolve.outputs.app-token" not in yaml


def test_workflow_uses_pick_ref(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    yaml = _consumer(tmp_path, "c")
    assert "actions/pick-ref@main" in yaml
    assert "ref: ${{ steps.pick.outputs.ref }}" in yaml
    assert "ref: ${{ needs.resolve.outputs.ref }}" in yaml
    # Under workflow_call github.repository is the caller's, so both checkouts pin it.
    assert yaml.count("repository: org/c") == 2
    assert "client-id: ${{ secrets.CI_PERMISSIONS_APP_CLIENT_ID }}" in yaml
    assert "app-private-key: ${{ secrets.CI_PERMISSIONS_APP_PRIVATE_KEY }}" in yaml


def test_orchestrator_basic(tmp_path: Path) -> None:
    """A's orchestrator calls B and C flat as reusable workflows, C after B."""
    _make_chain_abc(tmp_path)
    yaml = _orch(tmp_path)
    assert "name: Downstream runner (a)" in yaml
    assert "${{ needs.context.outputs.head-sha }}" in yaml
    assert "${{ needs.context.outputs.head-branch }}" in yaml
    assert "if: ${{ needs.context.outputs.ci-conclusion == 'success' }}" in yaml
    assert "group: trigger-downstream-runner-${{ github.event.workflow_run.head_sha }}" in yaml
    assert "cancel-in-progress: true" in yaml
    assert "dispatch-and-wait" not in yaml
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" in yaml
    assert "uses: org/c/.github/workflows/cross-repo-trigger.yml@develop" in yaml
    assert "secrets: inherit" in yaml
    assert "from-repo: ${{ github.repository }}" in yaml
    assert "from-sha: ${{ needs.context.outputs.head-sha }}" in yaml
    assert "branch: ${{ needs.context.outputs.head-branch }}" in yaml
    assert "fallback-ref: main" in yaml
    assert "fallback-ref: develop" in yaml
    assert "from-jobs:" in yaml
    assert '["a/build"]' in yaml
    assert "  validate:\n" in yaml
    assert "actions/validate-generated-workflows@main" in yaml
    assert "needs:\n    - context\n    - validate\n" in yaml
    assert "needs:\n    - context\n    - validate\n    - b\n" in yaml
    assert "actions/create-github-app-token@v3" in yaml
    assert "  report-start:\n" in yaml
    assert "  report-result:\n" in yaml
    assert "downstream/runner" in yaml


def test_orchestrator_returns_none_for_leaf(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    assert _render_orch(tmp_path, pkg="c") is None


def _make_chain_ab(tmp_path: Path, *, a_vis: str = "public", b_vis: str = "public", hpc: bool = False) -> None:
    """A -> B with per-repo visibility; `hpc` adds a build-hpc kind needing the upstream's."""

    def hpc_kind(needs: str) -> str:
        if not hpc:
            return ""
        return f"""
        [matrix.build-hpc]
        execution = "hpc"
        triggers = ["upstream-change", "rebuild-request"]
        job-script = "./.ci/hpc/build.sh"
        needs = {needs}
        [[matrix.build-hpc.include]]
        runs-on = "hpc"
        site = "hpc-batch"
        job-script = "./.ci/hpc/build.sh"
        """

    write_repo(
        tmp_path,
        "a",
        f"""
        [package]
        name = "a"
        repo = "org/a"
        visibility = "{a_vis}"
        compiler-inputs = []
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        {hpc_kind("[]")}
        """,
    )
    write_repo(
        tmp_path,
        "b",
        f"""
        [package]
        name = "b"
        repo = "org/b"
        visibility = "{b_vis}"
        compiler-inputs = []
        [[deps]]
        repo = "org/a"
        package = "a"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        {hpc_kind('["a/build-hpc"]')}
        """,
    )


def _build_checkouts(tmp_path: Path, lane: Execution) -> list[dict[str, Any]]:
    doc = yaml.safe_load(_consumer(tmp_path, "a", lane))
    return [
        step["with"]
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses") == "actions/checkout@v6" and "needs.resolve" in str(step["with"].get("ref"))
    ]


@pytest.mark.parametrize("lane", [EXECUTION_RUNNER, EXECUTION_HPC])
def test_submodules_reach_the_build_checkout(tmp_path: Path, lane: Execution) -> None:
    _make_chain_ab(tmp_path, hpc=True)
    manifest = tmp_path / "a" / ".ci" / "manifest.toml"
    manifest.write_text(
        manifest.read_text().replace("compiler-inputs = []", 'compiler-inputs = []\nsubmodules = "recursive"', 1)
    )
    checkouts = _build_checkouts(tmp_path, lane)
    assert checkouts and all(c["submodules"] == "recursive" for c in checkouts)


def test_no_submodules_key_by_default(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, hpc=True)
    for lane in (EXECUTION_RUNNER, EXECUTION_HPC):
        checkouts = _build_checkouts(tmp_path, lane)
        assert checkouts and not any("submodules" in c for c in checkouts)


def test_visibility_parses_explicit_values(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    assert by_pkg["a"].visibility == "public"
    assert by_pkg["b"].visibility == "private"


def test_visibility_absent_is_private(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    for m in parse_all(tmp_path):
        assert m.visibility == "private"


def test_public_upstream_dispatches_private_consumer(tmp_path: Path) -> None:
    """`uses:` would render the private repo's jobs into the public run."""
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    yaml = _orch(tmp_path)
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" not in yaml
    assert "actions/dispatch-and-wait@main" in yaml
    assert "consumer-repo: org/b" in yaml
    assert "artifact-names: ''" in yaml
    assert "wait-for-run-conclusion: 'true'" in yaml
    assert "from-jobs:" in yaml
    assert '["a/build"]' in yaml


@pytest.mark.parametrize(("a_vis", "b_vis"), [("private", "private"), ("private", "public")])
def test_private_upstream_uses_workflow_call(tmp_path: Path, a_vis: str, b_vis: str) -> None:
    _make_chain_ab(tmp_path, a_vis=a_vis, b_vis=b_vis)
    yaml = _orch(tmp_path)
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" in yaml
    assert "dispatch-and-wait" not in yaml


def test_kind_job_posts_check_run_on_dispatch(tmp_path: Path) -> None:
    """Only a workflow_dispatch with from-jobs reports a check run to the dispatcher."""
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    yaml = _consumer(tmp_path, "b")
    assert "actions/report-check-run@main" in yaml
    assert "phase: start" in yaml
    assert "phase: finish" in yaml
    assert "conclusion: ${{ job.status }}" in yaml
    assert "github.event_name == 'workflow_dispatch' && inputs.from-jobs != '[]'" in yaml
    assert "head-repo: ${{ inputs.from-repo }}" in yaml
    assert "details-url: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" in yaml


def test_kind_job_announces_its_image_before_the_checkout(tmp_path: Path) -> None:
    """Announced before checkout, so a checkout failure still names the image."""
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    for lane in (EXECUTION_RUNNER, EXECUTION_HPC):
        rendered = render_workflow(by_pkg["b"], by_pkg, lane=lane)
        if rendered is None:
            continue
        for job_name, job in yaml.safe_load(rendered)["jobs"].items():
            uses = [s.get("uses", "") for s in job.get("steps") or []]
            if "ecmwf/ci-infrastructure/actions/announce-image@main" not in uses:
                continue
            announce = uses.index("ecmwf/ci-infrastructure/actions/announce-image@main")
            checkouts = [i for i, u in enumerate(uses) if u.startswith("actions/checkout@")]
            assert checkouts, f"{lane}/{job_name}: expected a checkout step"
            assert announce < checkouts[0], f"{lane}/{job_name}: image announced after the checkout"


def test_announce_image_action_needs_nothing_from_this_repo() -> None:
    """Pure bash: no bootstrap, no checkout, no nested action."""
    action = Path(__file__).resolve().parents[1] / "actions" / "announce-image" / "action.yml"
    steps = yaml.safe_load(action.read_text())["runs"]["steps"]
    assert [s for s in steps if "run" in s], "expected an inline script"
    assert not [s for s in steps if "uses" in s], "announce-image must not compose another action"
    script = "\n".join(s.get("run", "") for s in steps)
    for forbidden in ("ensure-infrastructure-present", "CI_INFRASTRUCTURE_PYTHON", "pip "):
        assert forbidden not in script, f"{forbidden} reintroduces a bootstrap dependency"


def test_dispatch_and_wait_never_ingests_remote_logs() -> None:
    action = Path(__file__).resolve().parents[1] / "actions" / "dispatch-and-wait" / "action.yml"
    text = action.read_text()
    assert "run view" not in text
    assert "--log" not in text


def test_resolve_consumer_refs_picks_chain_refs(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    by_repo = {m.repo: m for m in parse_all(tmp_path)}
    assert resolve_consumer_refs(by_repo["org/a"], by_repo) == {"org/b": "main", "org/c": "develop"}


def test_resolve_consumer_refs_disagreement_errors(tmp_path: Path) -> None:
    """Two paths reach D with different refs."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        [[trigger-downstream]]
        repo = "org/c"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    for name, ref in (("b", "main"), ("c", "develop")):
        write_repo(
            tmp_path,
            name,
            f"""
            [package]
            name = "{name}"
            prefix = "{name}"
            repo = "org/{name}"
            compiler-inputs = []
            [[deps]]
            repo = "org/a"
            package = "a"
            [[trigger-downstream]]
            repo = "org/d"
            ref = "{ref}"
            [matrix.build]
            triggers = ["upstream-change", "rebuild-request"]
            action = "./.github/actions/build"
            needs = ["a/build"]
            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
        )
    write_repo(
        tmp_path,
        "d",
        """
        [[deps]]
        repo = "org/b"
        package = "b"
        [[deps]]
        repo = "org/c"
        package = "c"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["b/build", "c/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_repo = {m.repo: m for m in manifests}
    with pytest.raises(SchemaError, match="ref disagreement"):
        resolve_consumer_refs(by_repo["org/a"], by_repo)


def test_orchestrator_emits_one_job_per_consumer_with_all_originator_kinds(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        [matrix.test]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["build"]
        [[matrix.test.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build", "a/test"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    doc = yaml.safe_load(_orch(tmp_path))
    assert sorted(doc["jobs"]) == ["b", "context", "report-ci-failure", "report-result", "report-start", "validate"]
    assert json.loads(doc["jobs"]["b"]["with"]["from-jobs"]) == ["a/build", "a/test"]
    assert doc["jobs"]["b"]["name"] == "b"


def test_api_only_jobs_run_on_the_slim_runner(tmp_path: Path) -> None:
    """Jobs that only call APIs use SLIM_RUNNER; kind jobs keep the leg's runs-on."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "arc-sandbox-cci2"
        """,
    )
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build"]
        [[matrix.build.include]]
        runs-on = "arc-sandbox-cci2"
        """,
    )
    consumer = yaml.safe_load(_consumer(tmp_path, "b"))
    assert consumer["jobs"]["resolve"]["runs-on"] == SLIM_RUNNER
    assert consumer["jobs"]["b__build"]["runs-on"] == "${{ matrix['runs-on'] }}"
    assert yaml.safe_load(_orch(tmp_path))["jobs"]["validate"]["runs-on"] == SLIM_RUNNER


def test_orchestrator_orders_per_consumer(tmp_path: Path) -> None:
    """One caller job per consumer carrying every originator kind; c's job needs b's."""
    kinds = """
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = {build}
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        [matrix.build-hpc]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = {build_hpc}
        [[matrix.build-hpc.include]]
        runs-on = "ubuntu-latest"
        """
    write_repo(
        tmp_path,
        "a",
        '\n        [[trigger-downstream]]\n        repo = "org/b"\n        ref = "main"'
        + kinds.format(build="[]", build_hpc="[]"),
    )
    write_repo(
        tmp_path,
        "b",
        '\n        [[deps]]\n        repo = "org/a"\n        package = "a"'
        + kinds.format(build='["a/build"]', build_hpc='["a/build-hpc"]')
        + '        [[trigger-downstream]]\n        repo = "org/c"\n        ref = "main"\n',
    )
    write_repo(
        tmp_path,
        "c",
        '\n        [[deps]]\n        repo = "org/b"\n        package = "b"'
        + kinds.format(build='["b/build"]', build_hpc='["b/build-hpc"]'),
    )

    # build-hpc here is only a name; neither kind sets execution = "hpc".
    doc = yaml.safe_load(_orch(tmp_path))
    assert sorted(doc["jobs"]) == [
        "b",
        "c",
        "context",
        "report-ci-failure",
        "report-result",
        "report-start",
        "validate",
    ]
    assert doc["jobs"]["b"]["needs"] == ["context", "validate"]
    assert doc["jobs"]["c"]["needs"] == ["context", "validate", "b"]
    assert json.loads(doc["jobs"]["b"]["with"]["from-jobs"]) == ["a/build", "a/build-hpc"]
    assert json.loads(doc["jobs"]["c"]["with"]["from-jobs"]) == ["a/build", "a/build-hpc"]
    assert _render_orch(tmp_path, EXECUTION_HPC) is None


def _leaf_manifest(name: str, repo: str, *, hpc: bool = False) -> str:
    """A leaf producer with one runnable kind on one lane."""
    if hpc:
        return (
            f'[package]\nname = "{name}"\nrepo = "{repo}"\ncompiler-inputs = []\n'
            '[matrix.build-hpc]\nexecution = "hpc"\ntriggers = ["rebuild-request"]\n'
            'job-script = "./.ci/hpc/build.sh"\nneeds = []\n'
            '[[matrix.build-hpc.include]]\nruns-on = "hpc"\nsite = "hpc-batch"\n'
            'job-script = "./.ci/hpc/build.sh"\n'
        )
    return (
        f'[package]\nname = "{name}"\nrepo = "{repo}"\ncompiler-inputs = []\n'
        '[matrix.build]\ntriggers = ["rebuild-request"]\naction = "./.github/actions/build"\n'
        'needs = []\n[[matrix.build.include]]\nruns-on = "ubuntu-latest"\n'
    )


def test_render_workflow_none_for_absent_lane(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    assert render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_RUNNER) is not None
    assert render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_HPC) is None


def test_lane_split_consumer_files(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, hpc=True)
    runner = _consumer(tmp_path, "b", EXECUTION_RUNNER)
    hpc = _consumer(tmp_path, "b", EXECUTION_HPC)

    assert "matrix-build-hpc" not in runner
    assert "b__build:\n" in runner
    assert "matrix-build-hpc" in hpc
    assert "b__build_hpc:\n" in hpc
    assert "b__build:\n" not in hpc

    assert "group: cross-repo-trigger-runner-b-${{ github.ref }}" in runner
    assert "group: cross-repo-trigger-hpc-b-${{ github.ref }}" in hpc


def test_orchestrator_workflow_run_trigger_and_gate(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, hpc=True)
    for lane, suffix, label in ((EXECUTION_RUNNER, "", "runner"), (EXECUTION_HPC, "-hpc", "HPC")):
        orch = _orch(tmp_path, lane)
        doc = yaml.safe_load(orch)
        assert doc["name"] == f"Downstream {label} (a)"
        # PyYAML reads the bare key `on` as True.
        assert doc[True] == {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}}
        assert "inputs" not in orch
        gate = "${{ needs.context.outputs.ci-conclusion == 'success' }}"
        assert doc["jobs"]["validate"]["if"] == gate
        assert doc["jobs"]["report-start"]["if"] == gate
        assert doc["jobs"]["b"]["uses"].endswith(f"cross-repo-trigger{suffix}.yml@main")


def test_orchestrator_posts_commit_status(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, hpc=True)
    doc = yaml.safe_load(_orch(tmp_path))

    start = doc["jobs"]["report-start"]["steps"][-1]["run"]
    assert "gh api -X POST" in start
    assert "/repos/${{ github.repository }}/statuses/${{ needs.context.outputs.head-sha }}" in start
    assert "state=pending" in start
    assert "downstream/runner" in start

    result = doc["jobs"]["report-result"]
    assert result["needs"] == ["context", "validate", "b"]
    assert result["if"] == "${{ always() && needs.context.outputs.ci-conclusion == 'success' }}"
    run = result["steps"][-1]["run"]
    assert "${{ needs.validate.result }}" in run
    assert "${{ needs.b.result }}" in run
    assert "state=failure" in run
    assert "downstream/runner" in run

    orch_hpc = _orch(tmp_path, EXECUTION_HPC)
    assert "downstream/hpc" in orch_hpc
    assert "downstream/runner" not in orch_hpc


def test_orchestrator_posts_ci_failure_status(tmp_path: Path) -> None:
    """report-ci-failure fires on exactly the runs the success-gated jobs skip."""
    _make_chain_ab(tmp_path, hpc=True)
    for lane, context in ((EXECUTION_RUNNER, "downstream/runner"), (EXECUTION_HPC, "downstream/hpc")):
        job = yaml.safe_load(_orch(tmp_path, lane))["jobs"]["report-ci-failure"]
        assert job["if"] == "${{ needs.context.outputs.ci-conclusion != 'success' }}"
        run = job["steps"][-1]["run"]
        assert "gh api -X POST" in run
        assert "-f state=failure" in run
        assert f"-f context='{context}'" in run
        assert 'target_url="${{ needs.context.outputs.ci-url }}"' in run


def test_cross_package_deps_lane_scoped(tmp_path: Path) -> None:
    write_repo(tmp_path, "b", _leaf_manifest("b", "org/b"))
    write_repo(tmp_path, "d", _leaf_manifest("d", "org/d", hpc=True))
    write_repo(
        tmp_path,
        "c",
        """
        [matrix.build]
        triggers = ["upstream-change"]
        action = "./.github/actions/build"
        needs = ["b/build"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        [matrix.build-hpc]
        execution = "hpc"
        triggers = ["upstream-change"]
        job-script = "./.ci/hpc/build.sh"
        needs = ["d/build-hpc"]
        [[matrix.build-hpc.include]]
        runs-on = "hpc"
        site = "hpc-batch"
        job-script = "./.ci/hpc/build.sh"
        """,
    )
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    scope = ["b", "c", "d"]
    assert _cross_package_deps(scope, by_pkg, lane=EXECUTION_RUNNER) == {"b": set(), "c": {"b"}, "d": set()}
    assert _cross_package_deps(scope, by_pkg, lane=EXECUTION_HPC) == {"b": set(), "c": {"d"}, "d": set()}


def test_hpc_orchestrator_targets_hpc_files(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private", hpc=True)
    runner_with = yaml.safe_load(_orch(tmp_path))["jobs"]["b"]["steps"][-1]["with"]
    assert "workflow-file" not in runner_with
    hpc_with = yaml.safe_load(_orch(tmp_path, EXECUTION_HPC))["jobs"]["b"]["steps"][-1]["with"]
    assert hpc_with["workflow-file"] == "cross-repo-trigger-hpc.yml"


def _make_fanout(tmp_path: Path, n: int, legs: int) -> None:
    """a triggers c0..c<n-1>, each with `legs` distinct-platform legs."""
    triggers = "\n".join(
        f'        [[trigger-downstream]]\n        repo = "org/c{i}"\n        ref = "main"' for i in range(n)
    )
    write_repo(
        tmp_path,
        "a",
        f"""
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []
{triggers}
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    includes = "\n".join(
        f'            [[matrix.build.include]]\n            platform = "p{i}"\n            runs-on = "ubuntu-latest"'
        for i in range(legs)
    )
    for i in range(n):
        write_repo(
            tmp_path,
            f"c{i}",
            f"""
            [package]
            name = "c{i}"
            repo = "org/c{i}"
            compiler-inputs = []
            [[deps]]
            repo = "org/a"
            package = "a"
            [matrix.build]
            triggers = ["upstream-change", "rebuild-request"]
            action = "./.github/actions/build"
            needs = ["a/build"]
{includes}
            """,
        )


def test_orchestrator_caps_total_jobs(tmp_path: Path) -> None:
    _make_fanout(tmp_path, 5, 50)
    assert 5 * 50 > ORCHESTRATOR_MAX_TOTAL_JOBS
    with pytest.raises(SchemaError, match="exceeding the safety limit"):
        _render_orch(tmp_path)


def test_orchestrator_caps_reusable_workflows(tmp_path: Path) -> None:
    n = ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS + 1
    _make_fanout(tmp_path, n, 1)
    # caller + resolve + leg per consumer, plus 4 bookkeeping jobs: under the job cap.
    assert n * 3 + 4 < ORCHESTRATOR_MAX_TOTAL_JOBS
    with pytest.raises(SchemaError, match="reusable workflows"):
        _render_orch(tmp_path)


def _without_leading_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    body = [i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")]
    return "".join(lines[body[0] :])


_HEADER_MANIFEST: Final = """
    [generated]
    header = '''
    # SPDX-FileCopyrightText: 2026 ECMWF
    # SPDX-License-Identifier: Apache-2.0
    '''

    [matrix.build]
    triggers = ["upstream-change"]
    action = "./.github/actions/build"
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
    platform = "linux"
    """


def test_generated_header_precedes_the_do_not_edit_banner(tmp_path: Path) -> None:
    rendered = render_single(tmp_path, _HEADER_MANIFEST)
    assert rendered.startswith(
        "# SPDX-FileCopyrightText: 2026 ECMWF\n# SPDX-License-Identifier: Apache-2.0\n\n# GENERATED FILE"
    )
    assert yaml.safe_load(rendered) == yaml.safe_load(_without_leading_comments(rendered))


def test_generated_header_round_trips_through_check(tmp_path: Path) -> None:
    rendered = render_single(tmp_path, _HEADER_MANIFEST)
    out = tmp_path / "wf.yml"
    out.write_text(rendered)
    assert _write_or_check_path(out, rendered, check=True) == (False, [])
    out.write_text(_without_leading_comments(rendered))
    assert _write_or_check_path(out, rendered, check=True) == (False, [])


def test_validate_job_opts_into_the_fork_checkout(tmp_path: Path) -> None:
    """The fork head sha is refused by actions/checkout from a workflow_run otherwise."""
    _make_chain_abc(tmp_path)
    checkout = next(
        s
        for s in yaml.safe_load(_orch(tmp_path))["jobs"]["validate"]["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["allow-unsafe-pr-checkout"] is True

    # Consumer checkouts resolve to a branch of their own repo and need no opt-in.
    for job in yaml.safe_load(_consumer(tmp_path, "b"))["jobs"].values():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                assert "allow-unsafe-pr-checkout" not in (step.get("with") or {})


def _drift_repo(tmp_path: Path) -> Path:
    write_repo(tmp_path, "a", _HEADER_MANIFEST)
    wf = tmp_path / "a" / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "cross-repo-trigger.yml").write_text("name: stale\non:\n  workflow_call: {}\njobs: {}\n")
    return tmp_path / "a"


def test_check_warns_on_drift_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.chdir(_drift_repo(tmp_path))
    _run(".ci/manifest.toml", check=True, sibling_root=tmp_path)
    err = capsys.readouterr().err
    assert "::warning::generated files are out of date:" in err
    assert err.count("::warning::") == 1
    assert "cross-repo-trigger.yml" in err
    assert "This is a warning." in err


def test_check_fails_on_drift_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_drift_repo(tmp_path))
    with pytest.raises(CIError, match="generated files are out of date"):
        _run(".ci/manifest.toml", check=True, sibling_root=tmp_path, fail_on_drift=True)


def test_fail_on_drift_without_check_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_drift_repo(tmp_path))
    with pytest.raises(CIError, match="only applies with --check"):
        _run(".ci/manifest.toml", check=False, sibling_root=tmp_path, fail_on_drift=True)


def test_schema_violations_still_fail_in_warn_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        triggers = ["upstream-change"]
        action = "./.github/actions/build"
        needs = ["nonexistent-kind"]
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    monkeypatch.chdir(tmp_path / "a")
    with pytest.raises(CIError, match="nonexistent-kind"):
        _run(".ci/manifest.toml", check=True, sibling_root=tmp_path)


def test_write_or_check_path_modes(tmp_path: Path) -> None:
    out = tmp_path / ".github/workflows/cross-repo-trigger.yml"
    assert _write_or_check_path(out, "hello\n", check=False)[0] is True
    assert out.read_text() == "hello\n"
    assert _write_or_check_path(out, "hello\n", check=False)[0] is False
    assert _write_or_check_path(out, "different\n", check=True)[0] is True
    assert out.read_text() == "hello\n"
    # None deletes the file.
    assert _write_or_check_path(out, None, check=False)[0] is True
    assert not out.exists()
    assert _write_or_check_path(out, None, check=False)[0] is False


_RENDERED = "# GENERATED FILE - DO NOT EDIT.\nname: CI\non:\n  push: {}\njobs:\n  a:\n    runs-on: x\n"


def test_check_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    out = tmp_path / "wf.yml"
    out.write_text(_RENDERED.replace("name: CI\n", "# someone explained something here\n\nname: CI\n"))
    assert _write_or_check_path(out, _RENDERED, check=True) == (False, [])


def test_check_ignores_mapping_order(tmp_path: Path) -> None:
    out = tmp_path / "wf.yml"
    out.write_text("jobs:\n  a:\n    runs-on: x\non:\n  push: {}\nname: CI\n")
    assert _write_or_check_path(out, _RENDERED, check=True) == (False, [])


def test_check_reports_where_the_documents_differ(tmp_path: Path) -> None:
    out = tmp_path / "wf.yml"
    out.write_text(_RENDERED.replace("runs-on: x", "runs-on: y"))
    changed, where = _write_or_check_path(out, _RENDERED, check=True)
    assert changed is True
    assert where == [".jobs.a.runs-on: 'x' rendered, 'y' checked in"]


def test_check_names_the_on_key_not_the_yaml_1_1_boolean(tmp_path: Path) -> None:
    out = tmp_path / "wf.yml"
    out.write_text(_RENDERED.replace("push: {}", "pull_request: {}"))
    _, where = _write_or_check_path(out, _RENDERED, check=True)
    assert where and all(w.startswith(".on.") for w in where), where


def test_check_reports_an_unparseable_checked_in_file(tmp_path: Path) -> None:
    out = tmp_path / "wf.yml"
    out.write_text("jobs: [unclosed\n")
    changed, where = _write_or_check_path(out, _RENDERED, check=True)
    assert changed is True
    assert where and where[0].startswith("cannot parse the checked-in file:")


def test_write_restores_the_canonical_form(tmp_path: Path) -> None:
    """Unlike --check, a write removes a stray comment."""
    out = tmp_path / "wf.yml"
    out.write_text(_RENDERED.replace("name: CI\n", "# stray\nname: CI\n"))
    assert _write_or_check_path(out, _RENDERED, check=False) == (True, [])
    assert out.read_text() == _RENDERED


def test_local_sibling_layer_reads_clones_and_skips_missing(tmp_path: Path) -> None:
    (tmp_path / "upstream" / ".ci").mkdir(parents=True)
    (tmp_path / "upstream" / ".ci" / "manifest.toml").write_text('[package]\nname = "up"\n')

    got = _local_sibling_layer(
        [("ecmwf/upstream", "develop"), ("ecmwf/absent", "develop")],
        tmp_path,
        ".ci/manifest.toml",
    )

    assert got[("ecmwf/upstream", "develop")] == ('[package]\nname = "up"\n', False)
    assert got[("ecmwf/absent", "develop")] == (None, False)


def test_local_sibling_layer_ignores_the_ref(tmp_path: Path) -> None:
    """--sibling-root reads each clone's working tree."""
    (tmp_path / "up" / ".ci").mkdir(parents=True)
    (tmp_path / "up" / ".ci" / "manifest.toml").write_text("x = 1\n")

    for ref in ("develop", "some-feature-branch", "HEAD"):
        got = _local_sibling_layer([("ecmwf/up", ref)], tmp_path, ".ci/manifest.toml")
        assert got[("ecmwf/up", ref)] == ("x = 1\n", False)


def _trigger_manifest(name: str, repo: str, targets: list[str]) -> str:
    blocks = "".join(f'\n[[trigger-downstream]]\nrepo = "{t}"\nref = "main"\n' for t in targets)
    return (
        f'[package]\nname = "{name}"\nprefix = "{name}"\nrepo = "{repo}"\n'
        "compiler-inputs = []\n\n"
        '[[matrix.build.include]]\nbuild-type = "Release"\nplatform = "ubuntu-24.04"\n\n'
        '[matrix.build]\ntriggers = ["rebuild-request"]\n'
        'action = "./.github/actions/build-x"\nneeds = []\n' + blocks
    )


def test_warns_when_a_trigger_target_manifest_is_unreadable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    local = parse_manifest_text(_trigger_manifest("up", "ecmwf/up", ["ecmwf/absent"]), tmp_path / ".ci/manifest.toml")

    _fetch_sibling_manifests(local, None, ".ci/manifest.toml", sibling_root=tmp_path)

    err = capsys.readouterr().err
    assert "::warning::" in err
    assert "ecmwf/absent" in err
    assert "--sibling-root" in err


def test_no_warning_when_every_trigger_target_resolves(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "down" / ".ci").mkdir(parents=True)
    (tmp_path / "down" / ".ci" / "manifest.toml").write_text(_trigger_manifest("down", "ecmwf/down", []))
    local = parse_manifest_text(_trigger_manifest("up", "ecmwf/up", ["ecmwf/down"]), tmp_path / ".ci/manifest.toml")

    _fetch_sibling_manifests(local, None, ".ci/manifest.toml", sibling_root=tmp_path)

    assert "::warning::" not in capsys.readouterr().err


def test_decode_step_takes_the_leg_through_env_not_the_script(tmp_path: Path) -> None:
    """A quote in the leg must never become shell syntax."""
    rendered = render_single(
        tmp_path,
        """
        [matrix.build]
        triggers = ["upstream-change"]
        action = "./.github/actions/build-a"
        needs = []
        ctest = true
        ctest-args = "-L nightly -E 's_test|s_zombies' -j 8"

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        platform = "ubuntu-24.04"
        """,
    )
    decode = next(
        s
        for job in yaml.safe_load(rendered)["jobs"].values()
        for s in job.get("steps", [])
        if s.get("name") == "Decode matrix-leg"
    )
    assert decode["env"] == {"MATRIX_LEG": "${{ toJSON(matrix) }}"}
    assert 'leg="$MATRIX_LEG"' in decode["run"]
    assert "${{" not in decode["run"]


# === [downstream-gate] =====================================================

_GATE_UPSTREAM: Final = """
    [[trigger-downstream]]
    repo = "org/b"
    ref = "main"
    [downstream-gate]
    label = "run-downstream-CI"
    [matrix.build]
    triggers = ["upstream-change"]
    action = "./.github/actions/build"
    needs = []
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
"""

_UNGATED_UPSTREAM: Final = _GATE_UPSTREAM.replace('[downstream-gate]\n    label = "run-downstream-CI"\n', "")

_GATE_CONSUMER: Final = """
    [[deps]]
    repo = "org/a"
    package = "a"
    [matrix.build]
    triggers = ["upstream-change"]
    action = "./.github/actions/build"
    needs = ["a/build"]
    [[matrix.build.include]]
    runs-on = "ubuntu-latest"
"""


def _render_gate(tmp_path: Path, upstream: str) -> dict[Any, Any]:
    write_repo(tmp_path, "a", upstream)
    write_repo(tmp_path, "b", _GATE_CONSUMER)
    doc: dict[Any, Any] = yaml.safe_load(_orch(tmp_path))
    return doc


def test_downstream_gate_absent_by_default(tmp_path: Path) -> None:
    doc = _render_gate(tmp_path, _UNGATED_UPSTREAM)

    assert "label-gate" not in doc["jobs"]
    assert doc["jobs"]["validate"]["if"] == "${{ needs.context.outputs.ci-conclusion == 'success' }}"


def test_downstream_gate_fronts_every_job(tmp_path: Path) -> None:
    """Including report-start and report-ci-failure: an opted-out PR posts no status."""
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    assert doc["jobs"]["label-gate"]["outputs"] == {"run": "${{ steps.gate.outputs.run }}"}
    for jid, job in doc["jobs"].items():
        # context runs first: the gate looks its PR up by the commit context resolves.
        if jid in ("label-gate", "context"):
            continue
        assert "label-gate" in job["needs"], jid
        # Index syntax: `needs.label-gate` would parse the hyphen as minus.
        assert "needs['label-gate'].outputs.run == 'true'" in job["if"], jid


def test_downstream_gate_preserves_the_condition_it_wraps(tmp_path: Path) -> None:
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    assert doc["jobs"]["validate"]["if"] == (
        "${{ (needs.context.outputs.ci-conclusion == 'success') && needs['label-gate'].outputs.run == 'true' }}"
    )
    assert doc["jobs"]["report-ci-failure"]["if"] == (
        "${{ (needs.context.outputs.ci-conclusion != 'success') && needs['label-gate'].outputs.run == 'true' }}"
    )
    assert doc["jobs"]["report-result"]["if"].startswith("${{ (always() && ")


def test_downstream_gate_job_is_not_itself_gated_on_ci_success(tmp_path: Path) -> None:
    """report-ci-failure needs the gate on the failure path."""
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    assert "if" not in doc["jobs"]["label-gate"]


def test_downstream_gate_delegates_the_verdict_to_the_shared_action(tmp_path: Path) -> None:
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    steps = doc["jobs"]["label-gate"]["steps"]
    assert not any("run" in s for s in steps)
    gate = next(s for s in steps if s.get("id") == "gate")
    assert gate["uses"] == "ecmwf/ci-infrastructure/actions/check-pr-label@main"
    assert gate["with"]["label"] == "run-downstream-CI"
    assert gate["with"]["sha"] == "${{ needs.context.outputs.head-sha }}"


def test_downstream_gate_reads_its_own_repo_with_the_plain_token(tmp_path: Path) -> None:
    """An App token 403s on a non-public repo's PRs; github.token with pull-requests: read does not."""
    job = _render_gate(tmp_path, _GATE_UPSTREAM)["jobs"]["label-gate"]

    assert job["permissions"] == {"pull-requests": "read"}
    assert not any("create-github-app-token" in s.get("uses", "") for s in job["steps"])
    gate = next(s for s in job["steps"] if s.get("id") == "gate")
    assert "token" not in gate["with"]


def test_the_context_job_reads_its_own_actions_runs_with_the_plain_token(tmp_path: Path) -> None:
    job = _render_gate(tmp_path, _GATE_UPSTREAM)["jobs"]["context"]

    assert job["permissions"] == {"actions": "read"}
    assert not any("create-github-app-token" in s.get("uses", "") for s in job["steps"])
    ctx = next(s for s in job["steps"] if s.get("id") == "ctx")
    assert "with" not in ctx


@pytest.mark.parametrize("jid", ["report-start", "report-ci-failure", "report-result"])
def test_status_jobs_post_to_their_own_repo_with_the_plain_token(tmp_path: Path, jid: str) -> None:
    job = _render_gate(tmp_path, _GATE_UPSTREAM)["jobs"][jid]

    assert job["permissions"] == {"statuses": "write"}
    assert not any("create-github-app-token" in s.get("uses", "") for s in job["steps"])
    (step,) = job["steps"]
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_cross_repo_jobs_keep_the_app_token(tmp_path: Path) -> None:
    """validate reads sibling repos, which github.token cannot."""
    job = _render_gate(tmp_path, _GATE_UPSTREAM)["jobs"]["validate"]

    assert any("create-github-app-token" in s.get("uses", "") for s in job["steps"])
    assert "permissions" not in job


def test_downstream_gate_label_never_becomes_shell_syntax(tmp_path: Path) -> None:
    """The label travels as an action input, not shell text."""
    doc = _render_gate(tmp_path, _GATE_UPSTREAM.replace("run-downstream-CI", "it's-needed"))

    gate = next(s for s in doc["jobs"]["label-gate"]["steps"] if s.get("id") == "gate")
    assert gate["with"]["label"] == "it's-needed"


def test_a_completed_ci_run_is_the_only_entry_point(tmp_path: Path) -> None:
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    assert doc[True] == {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}}


def test_a_gate_label_adds_no_trigger(tmp_path: Path) -> None:
    """The label is read by label-gate, not by an `on:` filter."""
    without = _render_gate(tmp_path, _UNGATED_UPSTREAM)

    assert without[True] == _render_gate(tmp_path, _GATE_UPSTREAM)[True]


def test_no_ci_approval_gate(tmp_path: Path) -> None:
    """Under workflow_run the approval gate would always pass; a green CI for the head SHA is the gate."""
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    assert "ci-approval" not in doc["jobs"]
    assert "require-ci-approval" not in yaml.dump(doc)
    for jid, job in doc["jobs"].items():
        if jid == "context":
            continue
        assert "context" in job["needs"], jid


def test_the_label_filter_sits_only_on_the_label_gate_job(tmp_path: Path) -> None:
    """A job skipped by `if:` reports Success, so work jobs read label-gate's output."""
    doc = _render_gate(tmp_path, _GATE_UPSTREAM)

    for job in doc["jobs"].values():
        assert "github.event.label" not in (job.get("if") or "")


def test_the_commit_comes_from_the_context_job_everywhere(tmp_path: Path) -> None:
    """Only the concurrency key (which cannot see `needs`) reads workflow_run directly."""
    write_repo(tmp_path, "a", _GATE_UPSTREAM)
    write_repo(tmp_path, "b", _GATE_CONSUMER)
    orch = _orch(tmp_path)

    doc: dict[str, Any] = yaml.safe_load(orch)
    assert doc["concurrency"]["group"] == "trigger-downstream-runner-${{ github.event.workflow_run.head_sha }}"
    assert orch.count("github.event.workflow_run") == 1

    ctx = doc["jobs"]["context"]
    assert "needs" not in ctx
    assert sorted(ctx["outputs"]) == ["ci-conclusion", "ci-summary", "ci-url", "head-branch", "head-sha"]
    resolve = next(s for s in ctx["steps"] if s.get("id") == "ctx")
    assert resolve["uses"] == "ecmwf/ci-infrastructure/actions/resolve-dispatch-context@main"


# === Artifact identity is the artifact-name projection =====================


def _pkg(compiler_inputs: str, legs: str, publishes: bool = True) -> str:
    return f"""
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = {compiler_inputs}

        [matrix.build]
        action = "./.github/actions/build"
        {"" if publishes else "publishes = false"}
        {legs}
        """


_SCHEDULING_ONLY_LEGS: Final = """
        [[matrix.build.include]]
        cxx-compiler = "g++-13"
        build-type = "Release"
        runs-on = "arc"
        container = "img-a:1"
        platform = "ubuntu-24.04"

        [[matrix.build.include]]
        cxx-compiler = "g++-13"
        build-type = "Release"
        runs-on = "ubuntu-24.04"
        platform = "{host_platform}"
        """


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        pytest.param(
            _pkg('["cxx-compiler"]', _SCHEDULING_ONLY_LEGS.format(host_platform="ubuntu-24.04")),
            "same artifact identity",
            id="differ-only-in-runs-on-and-container",
        ),
        pytest.param(
            _pkg('["cxx-compiler"]', _SCHEDULING_ONLY_LEGS.format(host_platform="gh-ubuntu-24.04")),
            None,
            id="distinct-platform",
        ),
        pytest.param(
            _pkg('["cxx-compiler"]', _SCHEDULING_ONLY_LEGS.format(host_platform="ubuntu-24.04"), publishes=False),
            None,
            id="non-publishing-kind-may-collide",
        ),
        pytest.param(
            _pkg(
                '["cxx-compiler"]',
                """
        [[matrix.build.include]]
        cxx-compiler = "g++-8"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        modules = ["load gcc/old"]
        cc = "gcc"

        [[matrix.build.include]]
        cxx-compiler = "g++-8"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        modules = ["load gcc/11"]
        cc = "gcc"
        """,
            ),
            "(?s)same artifact identity.*differ only in.*modules",
            id="differ-only-in-toolchain-keys",
        ),
        pytest.param(
            _pkg(
                "[]",
                """
        [[matrix.build.include]]
        build-type = "Release"
        platform = "hpc-atos-gnu"
        job-script = "./.ci/hpc/build-gnu.sh"

        [[matrix.build.include]]
        build-type = "Release"
        platform = "hpc-atos-gnu"
        job-script = "./.ci/hpc/build-geo.sh"
        """,
            ),
            "same artifact identity",
            id="differ-only-in-job-script",
        ),
        pytest.param(
            _pkg(
                "[]",
                """
        [[matrix.build.include]]
        build-type = "Release"
        platform = "hpc-atos-gnu"
        modules = ["load prgenv/gnu"]

        [[matrix.build.include]]
        build-type = "Release"
        platform = "hpc-atos-intel"
        modules = ["load prgenv/intel-llvm"]
        """,
            ),
            None,
            id="toolchains-distinguished-by-platform",
        ),
        pytest.param(
            _pkg(
                '["cxx-compiler"]',
                """
        [[matrix.build.include]]
        cxx-compiler = "g++-8"
        build-type = "Release"
        platform = "hpc-atos-gnu"

        [[matrix.build.include]]
        cxx-compiler = "g++-8"
        build-type = "Release"
        platform = "hpc-atos-gnu"
        options = "eckit-geo"
        """,
            ),
            None,
            id="options-alone-distinguish",
        ),
        pytest.param(
            _pkg(
                "[]",
                """
        [[matrix.build.include]]
        platform = "ubuntu-24.04"

        [[matrix.build.include]]
        build-type = "Release"
        platform = "ubuntu-24.04"
        """,
            ),
            "same artifact identity",
            id="absent-build-type-is-release",
        ),
    ],
)
def test_leg_artifact_identity(tmp_path: Path, manifest: str, match: str | None) -> None:
    write_repo(tmp_path, "a", manifest)
    if match is None:
        validate_graph(parse_all(tmp_path))
    else:
        with pytest.raises(SchemaError, match=match):
            validate_graph(parse_all(tmp_path))
