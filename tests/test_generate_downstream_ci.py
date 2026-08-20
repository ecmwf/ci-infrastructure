# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ci_infrastructure.generate_downstream_ci.

Covers each consistency-check failure mode (trigger cycle, subset violation,
dangling local and cross-repo needs, reachability, colliding artifact identity,
the orchestrator job/reusable-workflow caps) and the shape of both generated
workflows per lane — including which edges dispatch rather than `uses:` a
consumer, since that is what keeps a private repo's logs out of a public run.

Fixtures are built with `write_repo` / `parse_all` from conftest.py, which
supply the [package] block a test is not about.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import parse_all, write_repo

from ci_infrastructure.generate_downstream_ci import (
    EXECUTION_HPC,
    EXECUTION_RUNNER,
    ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS,
    ORCHESTRATOR_MAX_TOTAL_JOBS,
    SLIM_RUNNER,
    SchemaError,
    _cross_package_deps,
    _fetch_sibling_manifests,
    _local_sibling_layer,
    _write_or_check_path,
    compute_transitive_consumers,
    parse_manifest_text,
    render_orchestrator_workflow,
    render_workflow,
    resolve_consumer_refs,
    transitive_cross_repo_needs,
    validate_graph,
)


def test_unknown_matrix_key_rejected(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        bogus = "x"

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="unknown key"):
        parse_all(tmp_path)


def test_action_required_when_kind_triggered(tmp_path: Path) -> None:
    """A kind that opts into any trigger must declare `action`; otherwise the
    generated cross-repo-trigger.yml has nothing to invoke."""
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        triggers = ["rebuild-request"]
        # no action — should fail
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="has no `action`"):
        parse_all(tmp_path)


def test_action_path_must_be_local_composite(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        triggers = ["rebuild-request"]
        action = "../etc/passwd"
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="local composite path"):
        parse_all(tmp_path)


def test_forwarded_input_typo_rejected(tmp_path: Path) -> None:
    """A `forwarded-inputs` entry must appear in at least one [[matrix.<kind>.include]]
    leg; a typo is caught at parse time rather than producing an empty
    `require` failure at workflow runtime."""
    write_repo(
        tmp_path,
        "a",
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
    )
    with pytest.raises(SchemaError, match="typoed-field"):
        parse_all(tmp_path)


def test_artifact_prefix_must_be_non_empty(tmp_path: Path) -> None:
    """A kind that sets `artifact-prefix` must give a non-empty, identifier-shaped
    string. Empty/whitespace would corrupt downstream artifact names; characters
    outside the artifact-name alphabet break gh-api lookups."""
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        artifact-prefix = ""
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a"
        needs = []

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="artifact-prefix must be a non-empty string"):
        parse_all(tmp_path)


def test_artifact_prefix_is_an_accepted_kind_key(tmp_path: Path) -> None:
    """A kind may declare `artifact-prefix` (to publish a secondary artifact
    under its own name) without the generator rejecting it as an unknown key.

    The generator only validates the key's shape; resolve_deps reads the value
    from the manifest itself and applies it to the artifact name.
    """
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
    """A kind whose matrix legs declare python-version gets a Set up Python
    step between Decode and Fetch resolved deps, so fetch_deps.py's pip
    install runs on the matching interpreter (not the container's system
    Python). Without this step, cp310 wheels get rejected on a py3.12
    container."""
    write_repo(
        tmp_path,
        "a",
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
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_RUNNER)
    assert yaml is not None
    # The step exists, pulls version from the decoded matrix-leg output,
    # and sits between Decode and Fetch resolved deps.
    assert "uses: actions/setup-python@v6" in yaml
    assert "python-version: ${{ steps.m.outputs.python-version }}" in yaml
    decode_idx = yaml.index("Decode matrix-leg")
    setup_idx = yaml.index("Set up Python")
    fetch_idx = yaml.index("Fetch resolved deps")
    assert decode_idx < setup_idx < fetch_idx


def test_setup_python_omitted_when_no_leg_has_python_version(tmp_path: Path) -> None:
    """A kind whose legs don't declare python-version (i.e. a C++ build)
    must NOT get a Set up Python step — there's nothing to set up, and an
    empty `python-version:` input would error out actions/setup-python."""
    write_repo(
        tmp_path,
        "a",
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
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "actions/setup-python" not in yaml
    assert "Set up Python" not in yaml


def test_job_name_includes_python_version_when_legs_vary_it(tmp_path: Path) -> None:
    """A kind whose legs vary python-version shows a py<version> slot in the job
    name (and thus in the check runs reported back to the dispatcher), even when
    another field — here cxx-compiler, which sorts first — is the primary
    distinguisher. Otherwise legs on the same compiler/platform but different
    python are indistinguishable in the Actions UI."""
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        prefix = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.build]
        triggers = ["rebuild-request"]
        action = "./.github/actions/build-a"
        forwarded-inputs = ["cxx-compiler", "python-version"]
        needs = []

        [[matrix.build.include]]
        cxx-compiler = "clang++-18"
        python-version = "3.10"
        platform = "ubuntu-24.04"
        runs-on = "ubuntu-latest"

        [[matrix.build.include]]
        cxx-compiler = "g++-13"
        python-version = "3.12"
        platform = "ubuntu-24.04"
        runs-on = "ubuntu-latest"
        """,
    )
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert (
        "name: a/build (${{ matrix['cxx-compiler'] }}, py${{ matrix.python-version }}, ${{ matrix.platform }})" in yaml
    )


def test_job_name_python_version_not_duplicated_when_sole_distinguisher(tmp_path: Path) -> None:
    """When python-version is itself the distinguishing field, it appears exactly
    once (py-prefixed) — never as both the primary slot and a second py<version>
    slot."""
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.test]
        triggers = ["rebuild-request"]
        action = "./.github/actions/test-a"
        forwarded-inputs = ["python-version"]
        publishes = false
        needs = []

        [[matrix.test.include]]
        python-version = "3.10"
        platform = "ubuntu-24.04"
        runs-on = "ubuntu-latest"

        [[matrix.test.include]]
        python-version = "3.12"
        platform = "ubuntu-24.04"
        runs-on = "ubuntu-latest"
        """,
    )
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "name: a/test (py${{ matrix.python-version }}, ${{ matrix.platform }})" in yaml


def test_workflow_inlines_build_action_not_downstream_job(tmp_path: Path) -> None:
    """The generated per-kind job calls the manifest-declared composite directly
    (no downstream-job intermediary); decode + fetch-deps + build + publish are
    inlined per the new render shape."""
    write_repo(
        tmp_path,
        "a",
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
    [m] = parse_all(tmp_path)
    yaml = render_workflow(m, {"a": m}, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "uses: ./.github/actions/build-thisrepo" in yaml
    assert "downstream-job" not in yaml  # the shim is gone
    # decode + fetch-deps + publish scaffolding all present
    assert "Decode matrix-leg" in yaml
    assert "command -v jq" in yaml
    assert "Fetch resolved deps" in yaml
    assert "mode: download-only" in yaml
    assert "mode: publish" in yaml
    # forwarded values get threaded through
    assert "cmake-prefix-path: ${{ steps.deps.outputs.cmake-prefix-path }}" in yaml
    assert "build-type: ${{ steps.m.outputs.build-type }}" in yaml


def test_trigger_downstream_rejects_unknown_keys(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        extra = "nope"
        """,
    )
    with pytest.raises(SchemaError, match="must define exactly 'repo' and 'ref'"):
        parse_all(tmp_path)


def test_trigger_downstream_requires_ref(tmp_path: Path) -> None:
    """`ref` is required as of PR 2 — orchestrator can't pin uses:@<ref> without it."""
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        """,
    )
    with pytest.raises(SchemaError, match="must define exactly 'repo' and 'ref'"):
        parse_all(tmp_path)


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


def test_duplicate_trigger_downstream(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"

        [[trigger-downstream]]
        repo = "org/b"
        ref = "main"
        """,
    )
    with pytest.raises(SchemaError, match="duplicate"):
        parse_all(tmp_path)


def test_reuse_matrix_target_missing(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.test]
        reuse-matrix = "build"
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["build"]
        """,
    )
    # parse_manifest itself raises this: reuse target doesn't exist.
    with pytest.raises(SchemaError, match="reuse-matrix"):
        parse_all(tmp_path)


def test_parse_manifest_text_round_trip(tmp_path: Path) -> None:
    """parse_manifest_text mirrors parse_manifest's behavior on the same TOML
    string — needed for --fetch mode where sibling manifests come from GraphQL
    blobs, not the filesystem.
    """
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

    # Mismatching schema surfaces SchemaError exactly like parse_manifest does.
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


def _make_two_repo_pair(tmp_path: Path, *, with_dep_back: bool) -> Path:
    """Helper: A triggers B; B optionally depends on A."""
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
    return tmp_path


def test_subset_invariant_violated(tmp_path: Path) -> None:
    """A triggers B but B doesn't list A as a dep."""
    _make_two_repo_pair(tmp_path, with_dep_back=False)
    with pytest.raises(SchemaError, match="does not list .* as a \\[\\[deps\\]\\]"):
        validate_graph(parse_all(tmp_path))


def test_happy_two_repo_pair(tmp_path: Path) -> None:
    _make_two_repo_pair(tmp_path, with_dep_back=True)
    # Should not raise
    validate_graph(parse_all(tmp_path))


def test_trigger_cycle(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [[deps]]
        repo = "org/b"
        package = "b"

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
        repo = "org/a"
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


def test_dangling_local_need(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
        """
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["nope"]

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="local kind 'nope'"):
        validate_graph(parse_all(tmp_path))


def test_colliding_publishing_legs_rejected(tmp_path: Path) -> None:
    # Two build legs that differ only in runs-on/container (scheduling, not
    # artifact identity) resolve to the same artifact name -> rejected.
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.build]
        action = "./.github/actions/build"

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
        platform = "ubuntu-24.04"
        """,
    )
    with pytest.raises(SchemaError, match="same artifact identity"):
        validate_graph(parse_all(tmp_path))


def test_distinct_platform_host_leg_ok(tmp_path: Path) -> None:
    # Same two legs, but the host leg gets its own platform -> no collision.
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.build]
        action = "./.github/actions/build"

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
        platform = "gh-ubuntu-24.04"
        """,
    )
    validate_graph(parse_all(tmp_path))


def test_colliding_legs_allowed_in_non_publishing_kind(tmp_path: Path) -> None:
    # A non-publishing kind uploads nothing, so same-identity legs are harmless.
    write_repo(
        tmp_path,
        "a",
        """
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = ["cxx-compiler"]

        [matrix.test]
        action = "./.github/actions/test"
        publishes = false

        [[matrix.test.include]]
        cxx-compiler = "g++-13"
        build-type = "Release"
        runs-on = "arc"
        container = "img-a:1"
        platform = "ubuntu-24.04"

        [[matrix.test.include]]
        cxx-compiler = "g++-13"
        build-type = "Release"
        runs-on = "ubuntu-24.04"
        platform = "ubuntu-24.04"
        """,
    )
    validate_graph(parse_all(tmp_path))


def test_dangling_cross_repo_need_unknown_package(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "b",
        """
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["ghost/build"]

        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    with pytest.raises(SchemaError, match="unknown package 'ghost'"):
        validate_graph(parse_all(tmp_path))


def test_cross_repo_need_target_not_runnable(tmp_path: Path) -> None:
    """B needs a/internal but [matrix.internal] in A has no 'upstream-change' trigger."""
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
    """B needs a/build but A doesn't trigger B -- dispatch never fires."""
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


def test_transitive_cross_repo_needs_recurses(tmp_path: Path) -> None:
    """A -> B -> C: C/build's transitive needs must include A/build, not just B/build."""
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
        ref = "main"
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
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    c = by_pkg["c"]
    refs = transitive_cross_repo_needs(c, "build", by_pkg)
    assert {(r.package, r.kind) for r in refs} == {("a", "build"), ("b", "build")}


def test_kind_filter_accepts_transitive_originator(tmp_path: Path) -> None:
    """A -> B -> C: C's rendered cross-repo-trigger.yml must accept BOTH a/build and
    b/build in its `if:` filter, in either dispatch or call mode."""
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
        ref = "main"
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
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    c = by_pkg["c"]
    yaml = render_workflow(c, by_pkg, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "contains(fromJSON(inputs.from-jobs), 'a/build')" in yaml
    assert "contains(fromJSON(inputs.from-jobs), 'b/build')" in yaml
    # Consumer-driven recovery dispatches set rebuild-request=true.
    assert "inputs.rebuild-request" in yaml


def test_chain_closure(tmp_path: Path) -> None:
    """A -> B -> C: closure for `a/build` must include both B and C."""
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
        ref = "main"
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
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    a_entry = closures["org/a"]["a/build"]
    assert a_entry["consumers"] == ["org/b", "org/c"]
    # Every consumer's runnable kind that transitively depends on a/build:
    assert a_entry["expected-checks"] == ["b/build", "c/build"]
    b_entry = closures["org/b"]["b/build"]
    assert b_entry["consumers"] == ["org/c"]
    assert b_entry["expected-checks"] == ["c/build"]
    # c has nothing to dispatch to:
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
    closures = compute_transitive_consumers(manifests)
    b_entry = closures["org/b"]["b/build"]
    assert b_entry["consumers"] == ["org/c", "org/d", "org/e"]
    # c and d depend on b directly; e depends on c and d, so it transitively
    # depends on b too -- e/test must be in the expected-checks set.
    assert b_entry["expected-checks"] == ["c/build", "d/build", "e/test"]


def test_external_trigger_pruned(tmp_path: Path) -> None:
    """Triggers pointing at an out-of-scope repo are silently dropped from the closure."""
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
    closures = compute_transitive_consumers(manifests)
    # external-org/external-repo is dropped (we have no manifest for it).
    entry = closures["org/a"]["a/build"]
    assert entry["consumers"] == ["org/b"]
    assert entry["expected-checks"] == ["b/build"]


def _make_chain_abc(tmp_path: Path) -> None:
    """A -> B -> C trigger chain, B and C with one runnable build matrix each.
    Used by several call-mode tests."""
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


def test_workflow_dispatch_trigger(tmp_path: Path) -> None:
    """Generated cross-repo-trigger.yml exposes BOTH entry points — workflow_call
    (orchestrator) and workflow_dispatch (resolve_deps recovery) — sharing typed
    inputs. dispatch-id is dispatch-only and optional so the call path stays valid."""
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    yaml = render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_RUNNER)
    assert yaml is not None
    # Both triggers present under `on:` (workflow_call sorts first).
    assert "  workflow_call:\n" in yaml
    assert "  workflow_dispatch:\n" in yaml
    assert "dispatch-id:\n" in yaml
    assert "from-repo:\n" in yaml
    assert "from-sha:\n" in yaml
    assert "from-jobs:\n" in yaml
    assert "rebuild-request:\n" in yaml
    assert "branch:\n" in yaml
    assert "fallback-ref:\n" in yaml
    # dispatch-id is optional (workflow_call never sets it); defaulted to empty.
    assert "default: ''" in yaml
    # run-name embeds dispatch-id for the dispatch-path correlation.
    assert "run-name:" in yaml
    assert "${{ inputs.dispatch-id }}" in yaml
    # Per-kind filters opt into trigger conditions via the manifest's
    # `triggers` list; both directions are present in the rendered filters.
    assert "contains(fromJSON(inputs.from-jobs)," in yaml
    assert "inputs.rebuild-request" in yaml
    assert "client_payload" not in yaml
    assert "repository_dispatch" not in yaml
    # Concurrency keyed on a STATIC package token (not github.workflow, which
    # under workflow_call resolves to the caller and would deadlock against the
    # top-level run). Per-ref so branches don't serialize against each other.
    assert "concurrency:" in yaml
    # Group now encodes the lane so the runner and hpc files never collide.
    assert "group: cross-repo-trigger-runner-c-${{ github.ref }}" in yaml
    assert "${{ github.workflow }}-${{ github.ref }}" not in yaml
    assert "cancel-in-progress: false" in yaml
    # Pre-rename input names must be gone.
    assert "upstream-repo" not in yaml
    assert "upstream-sha" not in yaml
    assert "upstream-job" not in yaml


def test_workflow_mints_app_token_per_job(tmp_path: Path) -> None:
    """Every job that touches cross-repo APIs mints its OWN App token at the
    top. We can't share the resolve job's mint via outputs because GitHub
    Actions redacts ::add-mask::'d values when they flow through
    needs.<job>.outputs.<x> to downstream jobs (community/13082), leaving
    consumers with empty strings and a cascade of 404s. No ORG_READ_TOKEN
    fallback anywhere — the App is the single source of cross-repo access."""
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    yaml = render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_RUNNER)
    assert yaml is not None
    # Pre-rename diagnostics still gone.
    assert "post-upstream-check" not in yaml
    assert "Post check on upstream" not in yaml
    # At least 2 mint steps: one in resolve, one per kind job (chain-abc has
    # at least one kind in c).
    assert yaml.count("actions/create-github-app-token@v3") >= 2
    # Every token: input in a kind job points at the local mint, not at a
    # cross-job output reference (which would be redacted).
    assert "${{ steps.mint.outputs.token }}" in yaml
    assert "needs.resolve.outputs.app-token" not in yaml
    # The PAT fallback is gone.
    assert "ORG_READ_TOKEN" not in yaml


def test_workflow_uses_pick_ref(tmp_path: Path) -> None:
    """The resolve job runs pick-ref before checkout + resolve-deps for branch matching."""
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    yaml = render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "actions/pick-ref@main" in yaml
    # Pick-ref output drives both resolve checkout and per-kind checkout.
    assert "ref: ${{ steps.pick.outputs.ref }}" in yaml
    assert "ref: ${{ needs.resolve.outputs.ref }}" in yaml
    # Every checkout pins our OWN repo explicitly + a token. Under workflow_call
    # github.repository is the caller's, so a bare checkout would clone the
    # upstream orchestrator and resolve-deps would read the wrong manifest.
    assert "repository: org/c" in yaml
    # Both checkouts (resolve + each kind) carry the explicit repository.
    assert yaml.count("repository: org/c") == 2
    # Per-kind filter reads inputs.from-jobs.
    assert "contains(fromJSON(inputs.from-jobs)," in yaml
    # App-token plumbing wired into the resolve step so resolve-deps can dispatch
    # producer rebuilds for stale upstream artifacts.
    assert "client-id: ${{ secrets.CI_PERMISSIONS_APP_CLIENT_ID }}" in yaml
    assert "app-private-key: ${{ secrets.CI_PERMISSIONS_APP_PRIVATE_KEY }}" in yaml


def test_orchestrator_basic(tmp_path: Path) -> None:
    """A's orchestrator calls every transitive consumer (B, C) flat as reusable
    workflows, with cross-pkg needs preserving build order."""
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    yaml = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "name: Downstream runner (a)" in yaml
    # Top-level workflow_run: fires when this repo's `CI` workflow completes, as its
    # own Actions-tab run (no longer nested via ci.yml's workflow_call).
    assert "on:\n  workflow_run:\n" in yaml
    assert "workflows:\n    - CI\n" in yaml
    assert "types:\n    - completed\n" in yaml
    # SHA/branch now come from the workflow_run event; the old ci.yml inputs are gone.
    assert "${{ github.event.workflow_run.head_sha }}" in yaml
    assert "${{ github.event.workflow_run.head_branch }}" in yaml
    assert "inputs.upstream-sha" not in yaml
    assert "inputs.upstream-branch" not in yaml
    # Root jobs gate on the upstream CI having succeeded.
    assert "if: ${{ github.event.workflow_run.conclusion == 'success' }}" in yaml
    # Coalesce re-runs for the same tested commit, keyed by lane.
    assert "group: trigger-downstream-runner-${{ github.event.workflow_run.head_sha }}" in yaml
    assert "cancel-in-progress: true" in yaml
    # Both B and C are invoked as reusable workflows (flat fan-out, not chained),
    # each pinned to its [[trigger-downstream]].ref, with secrets: inherit.
    assert "dispatch-and-wait" not in yaml
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" in yaml
    assert "uses: org/c/.github/workflows/cross-repo-trigger.yml@develop" in yaml
    assert "secrets: inherit" in yaml
    # The `with:` block carries the dispatcher metadata sourced from the event.
    assert "from-repo: ${{ github.repository }}" in yaml
    assert "from-sha: ${{ github.event.workflow_run.head_sha }}" in yaml
    assert "branch: ${{ github.event.workflow_run.head_branch }}" in yaml
    assert "fallback-ref: main" in yaml
    assert "fallback-ref: develop" in yaml
    # Each call passes the originator kinds as a JSON array via `from-jobs`; this is
    # the runner lane, so only the runner originator kind appears.
    assert "from-jobs:" in yaml
    assert '["a/build"]' in yaml
    # Every consumer call gates on `validate` (the manifest-drift guard that runs
    # first); C additionally depends on B at the orchestrator level.
    assert "  validate:\n" in yaml
    assert "actions/validate-generated-workflows@main" in yaml
    # PyYAML emits sequences in block style by default.
    assert "needs:\n    - validate\n" in yaml  # B has no cross-pkg deps; just validate.
    assert "needs:\n    - validate\n    - b\n" in yaml  # C depends on B too.
    # `validate` still mints its own App token; per-consumer dispatch mints are gone.
    assert "actions/create-github-app-token@v3" in yaml
    # Commit-status jobs post the required downstream/<lane> context back to the SHA.
    assert "  report-start:\n" in yaml
    assert "  report-result:\n" in yaml
    assert "downstream/runner" in yaml
    # Static org-level dispatch secret is gone.
    assert "ORG_DISPATCH_TOKEN" not in yaml
    # `upstream-*` dispatch inputs renamed to `from-*`; old names must be gone.
    assert "upstream-job:" not in yaml
    assert "upstream-repo:" not in yaml


def test_orchestrator_returns_none_for_leaf(tmp_path: Path) -> None:
    """A consumer with no further triggers gets no orchestrator file."""
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    # C has no consumers — render returns None.
    assert render_orchestrator_workflow(by_pkg["c"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER) is None


def _make_chain_ab(tmp_path: Path, *, a_vis: str = "public", b_vis: str = "public") -> None:
    """A -> B trigger chain with per-repo visibility, for the log-isolation tests."""
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
        """,
    )


def _orchestrator_for_a(tmp_path: Path) -> str:
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    yaml = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert yaml is not None
    return yaml


def test_visibility_parses_explicit_values(tmp_path: Path) -> None:
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    assert by_pkg["a"].visibility == "public"
    assert by_pkg["b"].visibility == "private"


def test_visibility_absent_is_private(tmp_path: Path) -> None:
    """Fail closed: an unlabelled repo defaults to private so a public upstream
    never exposes its logs."""
    _make_chain_abc(tmp_path)  # no visibility keys anywhere
    for m in parse_all(tmp_path):
        assert m.visibility == "private"


def test_invalid_visibility_rejected(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        "a",
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
    )
    with pytest.raises(SchemaError, match="visibility"):
        parse_all(tmp_path)


def test_public_upstream_dispatches_private_consumer(tmp_path: Path) -> None:
    """The leak case: a public upstream must NOT reach a private consumer via
    `uses:` (which would render the private repo's jobs into the public run).
    It dispatches instead, so the private run stays private."""
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    yaml = _orchestrator_for_a(tmp_path)
    # Private consumer b is dispatched, never called as a reusable workflow.
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" not in yaml
    assert "actions/dispatch-and-wait@main" in yaml
    assert "consumer-repo: org/b" in yaml
    # No artifact to wait for: the upstream consumes nothing back from downstream.
    assert "artifact-names: ''" in yaml
    # Instead it gates on the downstream run's conclusion (status only, no logs),
    # so this orchestrator job turns red/green with the private downstream.
    assert "wait-for-run-conclusion: 'true'" in yaml
    # Still forwards the originator coordinates so b's per-kind filter fires.
    assert "from-jobs:" in yaml
    assert '["a/build"]' in yaml


def test_private_to_private_uses_workflow_call(tmp_path: Path) -> None:
    """private -> private stays on the native reusable-workflow path (no polling):
    the run is never public while it contains a private repo's job."""
    _make_chain_ab(tmp_path, a_vis="private", b_vis="private")
    yaml = _orchestrator_for_a(tmp_path)
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" in yaml
    assert "dispatch-and-wait" not in yaml


def test_private_to_public_uses_workflow_call(tmp_path: Path) -> None:
    """private -> public also stays native: a public repo's jobs in a private run
    expose nothing."""
    _make_chain_ab(tmp_path, a_vis="private", b_vis="public")
    yaml = _orchestrator_for_a(tmp_path)
    assert "uses: org/b/.github/workflows/cross-repo-trigger.yml@main" in yaml
    assert "dispatch-and-wait" not in yaml


def test_kind_job_posts_check_run_on_dispatch(tmp_path: Path) -> None:
    """Every dispatched per-kind job reports a check run back to the dispatcher,
    gated on the workflow_dispatch entry point with an upstream-change coordinate
    (so reusable-workflow and rebuild-request paths stay silent)."""
    _make_chain_ab(tmp_path, a_vis="public", b_vis="private")
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    yaml = render_workflow(by_pkg["b"], by_pkg, lane=EXECUTION_RUNNER)
    assert yaml is not None
    assert "actions/report-check-run@main" in yaml
    assert "phase: start" in yaml
    assert "phase: finish" in yaml
    assert "conclusion: ${{ job.status }}" in yaml
    # Only fires when a public upstream dispatched us — not on workflow_call or
    # the empty-from-jobs rebuild-request recovery path.
    assert "github.event_name == 'workflow_dispatch' && inputs.from-jobs != '[]'" in yaml
    # The details URL points at THIS (private) run, behind auth.
    assert "head-repo: ${{ inputs.from-repo }}" in yaml
    assert "details-url: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" in yaml


def test_dispatch_and_wait_never_ingests_remote_logs() -> None:
    """Guardrail: dispatching a private consumer must surface only its run URL
    (and optionally wait on S3 artifacts) — never pull the dispatched run's logs
    into this (possibly public) orchestrator job, which would defeat the point."""
    action = Path(__file__).resolve().parents[1] / "actions" / "dispatch-and-wait" / "action.yml"
    text = action.read_text()
    assert "run view" not in text
    assert "--log" not in text


def test_resolve_consumer_refs_picks_chain_refs(tmp_path: Path) -> None:
    _make_chain_abc(tmp_path)
    manifests = parse_all(tmp_path)
    by_repo = {m.repo: m for m in manifests}
    refs = resolve_consumer_refs(by_repo["org/a"], by_repo)
    # B from A's [[trigger-downstream]] = main; C from B's = develop.
    assert refs == {"org/b": "main", "org/c": "develop"}


def test_resolve_consumer_refs_disagreement_errors(tmp_path: Path) -> None:
    """Diamond closure where two paths reach the same consumer with different refs."""
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
    write_repo(
        tmp_path,
        "b",
        """
        [[deps]]
        repo = "org/a"
        package = "a"
        [[trigger-downstream]]
        repo = "org/d"
        ref = "main"
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
        repo = "org/a"
        package = "a"
        [[trigger-downstream]]
        repo = "org/d"
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
    """A consumer reached by two originator kinds still gets ONE caller job.

    `from-jobs` carries every originator kind (here a/build AND a/test), so a
    single call wakes both of the consumer's chains — the per-kind `if:` matches
    the array with contains(). No per-kind job duplication, no kind suffix.
    """
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
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    orch = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert orch is not None
    doc = yaml.safe_load(orch)

    # validate + report-start + report-ci-failure + report-result bookkeeping jobs
    # flank the single consumer caller job. Both runner originator kinds reach b via
    # one call.
    assert sorted(doc["jobs"]) == ["b", "report-ci-failure", "report-result", "report-start", "validate"]
    assert json.loads(doc["jobs"]["b"]["with"]["from-jobs"]) == ["a/build", "a/test"]
    assert doc["jobs"]["b"]["name"] == "b"


def test_api_only_jobs_run_on_the_slim_runner(tmp_path: Path) -> None:
    """resolve, validate and dispatch never compile anything, so they belong on
    the cheap 1-CPU runner. Per-kind jobs keep the manifest's `runs-on`, which is
    where the real work happens and where ubuntu-slim would be wrong."""
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
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    rendered = render_workflow(by_pkg["b"], by_pkg, lane=EXECUTION_RUNNER)
    assert rendered is not None
    consumer = yaml.safe_load(rendered)
    assert consumer["jobs"]["resolve"]["runs-on"] == SLIM_RUNNER
    # The kind job builds — it must follow the matrix leg, not the slim runner.
    assert consumer["jobs"]["b__build"]["runs-on"] == "${{ matrix['runs-on'] }}"

    orch = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert orch is not None
    assert yaml.safe_load(orch)["jobs"]["validate"]["runs-on"] == SLIM_RUNNER


def test_orchestrator_orders_per_consumer(tmp_path: Path) -> None:
    """One caller job per consumer, ordered by cross-package dependency.

    c consumes b, and both are reached by a/build and a/build-hpc. Each gets a
    single caller job carrying both originator kinds in `from-jobs`; c's job
    `needs` b's (whole) job. Both of a consumer's kinds run in parallel inside
    its one dispatch — collapsed across kinds, so no per-kind orchestrator jobs.
    """
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
        [matrix.build-hpc]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build-hpc.include]]
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
        [matrix.build-hpc]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["a/build-hpc"]
        [[matrix.build-hpc.include]]
        runs-on = "ubuntu-latest"
        [[trigger-downstream]]
        repo = "org/c"
        ref = "main"
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
        [matrix.build-hpc]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = ["b/build-hpc"]
        [[matrix.build-hpc.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    # Both a/build and a/build-hpc are runner-execution kinds here (the name is just
    # a name; neither sets execution = "hpc"), so both land on the runner lane and a
    # single caller job per consumer carries both. Ordering (c needs b) is preserved,
    # flanked by the validate + report-start + report-ci-failure + report-result jobs.
    orch = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert orch is not None
    doc = yaml.safe_load(orch)
    assert sorted(doc["jobs"]) == ["b", "c", "report-ci-failure", "report-result", "report-start", "validate"]
    assert doc["jobs"]["b"]["needs"] == ["validate"]
    assert doc["jobs"]["c"]["needs"] == ["validate", "b"]
    assert json.loads(doc["jobs"]["b"]["with"]["from-jobs"]) == ["a/build", "a/build-hpc"]
    assert json.loads(doc["jobs"]["c"]["with"]["from-jobs"]) == ["a/build", "a/build-hpc"]

    # These are all runner kinds, so the hpc lane is empty and renders no file.
    assert render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_HPC) is None


def _make_two_lane_chain_ab(tmp_path: Path, *, a_vis: str = "public", b_vis: str = "public") -> None:
    """a -> b, each with a runner `build` kind AND a genuine hpc `build-hpc` kind
    (execution = 'hpc'). The lanes are self-contained: build needs the upstream's
    build, build-hpc needs the upstream's build-hpc. Used by the lane-split tests."""
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
        [matrix.build-hpc]
        execution = "hpc"
        triggers = ["upstream-change", "rebuild-request"]
        job-script = "./.ci/hpc/build.sh"
        needs = []
        [[matrix.build-hpc.include]]
        runs-on = "hpc"
        site = "hpc-batch"
        job-script = "./.ci/hpc/build.sh"
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
        [matrix.build-hpc]
        execution = "hpc"
        triggers = ["upstream-change", "rebuild-request"]
        job-script = "./.ci/hpc/build.sh"
        needs = ["a/build-hpc"]
        [[matrix.build-hpc.include]]
        runs-on = "hpc"
        site = "hpc-batch"
        job-script = "./.ci/hpc/build.sh"
        """,
    )


def _leaf_manifest(name: str, repo: str, *, hpc: bool = False) -> str:
    """A minimal leaf producer with a single runnable kind on one lane. Returned
    already dedented (write_repo's dedent is then a no-op)."""
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
    """A runner-only manifest yields a runner consumer file but None for the hpc
    lane; a two-lane manifest yields both, each carrying only its own lane's kind."""
    _make_chain_abc(tmp_path)  # all runner kinds
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}
    assert render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_RUNNER) is not None
    assert render_workflow(by_pkg["c"], by_pkg, lane=EXECUTION_HPC) is None


def test_lane_split_consumer_files(tmp_path: Path) -> None:
    """The two lanes render two files, each with only its lane's kind and a
    lane-scoped concurrency group, so the files never collide."""
    _make_two_lane_chain_ab(tmp_path)
    by_pkg = {m.package_name: m for m in parse_all(tmp_path)}

    runner = render_workflow(by_pkg["b"], by_pkg, lane=EXECUTION_RUNNER)
    hpc = render_workflow(by_pkg["b"], by_pkg, lane=EXECUTION_HPC)
    assert runner is not None and hpc is not None

    # Runner file: build only, hpc file: build-hpc only.
    assert "matrix-build-hpc" not in runner
    assert "b__build:\n" in runner
    assert "matrix-build-hpc" in hpc
    assert "b__build_hpc:\n" in hpc
    assert "b__build:\n" not in hpc

    # Lane-scoped concurrency groups.
    assert "group: cross-repo-trigger-runner-b-${{ github.ref }}" in runner
    assert "group: cross-repo-trigger-hpc-b-${{ github.ref }}" in hpc


def test_orchestrator_workflow_run_trigger_and_gate(tmp_path: Path) -> None:
    """Each lane's orchestrator is a top-level `workflow_run` (on `CI` completing)
    whose root jobs gate on the upstream CI having succeeded."""
    _make_two_lane_chain_ab(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    for lane, suffix, label in ((EXECUTION_RUNNER, "", "runner"), (EXECUTION_HPC, "-hpc", "HPC")):
        orch = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=lane)
        assert orch is not None
        doc = yaml.safe_load(orch)
        assert doc["name"] == f"Downstream {label} (a)"
        # `on:` parses as the YAML 1.1 boolean True key, so assert on the text.
        assert "on:\n  workflow_run:\n" in orch
        assert "workflows:\n    - CI\n" in orch
        assert "types:\n    - completed\n" in orch
        assert "inputs" not in orch
        # Root bookkeeping jobs are gated on CI success; the consumer job cascades
        # off `validate` (a skipped gate skips the consumer).
        gate = "${{ github.event.workflow_run.conclusion == 'success' }}"
        assert doc["jobs"]["validate"]["if"] == gate
        assert doc["jobs"]["report-start"]["if"] == gate
        # The consumer caller job references this lane's consumer file.
        assert doc["jobs"]["b"]["uses"].endswith(f"cross-repo-trigger{suffix}.yml@main")


def test_orchestrator_posts_commit_status(tmp_path: Path) -> None:
    """report-start posts a pending downstream/<lane> status; report-result posts
    the final status, aggregating validate + every consumer job's result."""
    _make_two_lane_chain_ab(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    orch = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert orch is not None
    doc = yaml.safe_load(orch)

    # Pending status posted up-front to the tested head SHA.
    start = doc["jobs"]["report-start"]["steps"][-1]["run"]
    assert "gh api -X POST" in start
    assert "/repos/${{ github.repository }}/statuses/${{ github.event.workflow_run.head_sha }}" in start
    assert "state=pending" in start
    assert "downstream/runner" in start

    # Final status runs always() (still gated on CI success) and depends on validate
    # plus the consumer caller job, mapping their results to success/failure.
    result = doc["jobs"]["report-result"]
    assert result["needs"] == ["validate", "b"]
    assert result["if"] == "${{ always() && github.event.workflow_run.conclusion == 'success' }}"
    run = result["steps"][-1]["run"]
    assert "${{ needs.validate.result }}" in run
    assert "${{ needs.b.result }}" in run
    assert "state=failure" in run
    assert "downstream/runner" in run

    # The hpc lane posts to its own context.
    orch_hpc = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_HPC)
    assert orch_hpc is not None
    assert "downstream/hpc" in orch_hpc
    assert "downstream/runner" not in orch_hpc


def test_orchestrator_posts_ci_failure_status(tmp_path: Path) -> None:
    """report-ci-failure posts a red downstream/<lane> status when the upstream CI did
    NOT succeed — the exact complement of the success gate, so the required check goes
    red instead of hanging at "Expected". Present on both lanes with the right context."""
    _make_two_lane_chain_ab(tmp_path)
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    for lane, context in ((EXECUTION_RUNNER, "downstream/runner"), (EXECUTION_HPC, "downstream/hpc")):
        orch = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=lane)
        assert orch is not None
        job = yaml.safe_load(orch)["jobs"]["report-ci-failure"]
        # Non-success gate: fires on exactly the runs the success-gated jobs skip.
        assert job["if"] == "${{ github.event.workflow_run.conclusion != 'success' }}"
        run = job["steps"][-1]["run"]
        assert "gh api -X POST" in run
        assert "-f state=failure" in run
        assert f"-f context='{context}'" in run
        # Links to the failed CI run itself, not this (do-nothing) orchestrator run.
        assert 'target_url="${{ github.event.workflow_run.html_url }}"' in run


def test_cross_package_deps_lane_scoped(tmp_path: Path) -> None:
    """A consumer's cross-package deps are computed per lane: its runner kind's
    upstream and its hpc kind's upstream are reported separately, never merged."""
    # c's runner build depends on b; its hpc build-hpc depends on d instead.
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
    runner = _cross_package_deps(scope, by_pkg, lane=EXECUTION_RUNNER)
    hpc = _cross_package_deps(scope, by_pkg, lane=EXECUTION_HPC)
    assert runner == {"b": set(), "c": {"b"}, "d": set()}
    assert hpc == {"b": set(), "c": {"d"}, "d": set()}


def test_hpc_orchestrator_targets_hpc_files(tmp_path: Path) -> None:
    """The hpc orchestrator references the consumer's cross-repo-trigger-hpc.yml
    (reusable-workflow path), and when it must dispatch a private consumer it sets
    dispatch-and-wait's workflow-file to the -hpc file."""
    # public -> private forces the dispatch path (log isolation).
    _make_two_lane_chain_ab(tmp_path, a_vis="public", b_vis="private")
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    # Runner dispatch uses the default (unsuffixed) workflow file — no override.
    orch_runner = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)
    assert orch_runner is not None
    runner_with = yaml.safe_load(orch_runner)["jobs"]["b"]["steps"][-1]["with"]
    assert "workflow-file" not in runner_with

    # HPC dispatch targets the -hpc workflow file explicitly.
    orch_hpc = render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_HPC)
    assert orch_hpc is not None
    hpc_with = yaml.safe_load(orch_hpc)["jobs"]["b"]["steps"][-1]["with"]
    assert hpc_with["workflow-file"] == "cross-repo-trigger-hpc.yml"


def test_orchestrator_caps_total_jobs(tmp_path: Path) -> None:
    """Closure with too many matrix legs blows the total-jobs cap."""
    # 5 consumers, each with a fat matrix — pushes total jobs over the limit.
    legs_per_consumer = 50
    triggers_block = "\n".join(
        f'        [[trigger-downstream]]\n        repo = "org/c{i}"\n        ref = "main"' for i in range(5)
    )
    write_repo(
        tmp_path,
        "a",
        f"""
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []
{triggers_block}
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    # Distinct platform per leg so they're legitimately distinct artifacts
    # (not a self-collision) while still producing legs_per_consumer jobs.
    legs = "\n".join(
        f'            [[matrix.build.include]]\n            platform = "p{i}"\n            runs-on = "ubuntu-latest"'
        for i in range(legs_per_consumer)
    )
    for i in range(5):
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
{legs}
            """,
        )
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    # Sanity: 5 consumers * 50 legs + bookkeeping > MAX_TOTAL_JOBS.
    assert 5 * legs_per_consumer > ORCHESTRATOR_MAX_TOTAL_JOBS
    with pytest.raises(SchemaError, match="exceeding the safety limit"):
        render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)


def test_orchestrator_caps_reusable_workflows(tmp_path: Path) -> None:
    """Too many distinct consumers blows GHA's 20-reusable-workflow cap even when
    each consumer's matrix is tiny enough to stay under the total-jobs cap."""
    n_consumers = ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS + 1  # 21
    triggers_block = "\n".join(
        f'        [[trigger-downstream]]\n        repo = "org/c{i}"\n        ref = "main"' for i in range(n_consumers)
    )
    write_repo(
        tmp_path,
        "a",
        f"""
        [package]
        name = "a"
        repo = "org/a"
        compiler-inputs = []
{triggers_block}
        [matrix.build]
        triggers = ["upstream-change", "rebuild-request"]
        action = "./.github/actions/build"
        needs = []
        [[matrix.build.include]]
        runs-on = "ubuntu-latest"
        """,
    )
    # One leg per consumer: 21 * (resolve + leg + caller) + (validate + report-start
    # + report-result) ~= 66 jobs, well under the 220 job cap, so the
    # reusable-workflow cap trips first.
    for i in range(n_consumers):
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
            [[matrix.build.include]]
            runs-on = "ubuntu-latest"
            """,
        )
    manifests = parse_all(tmp_path)
    validate_graph(manifests)
    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}
    # Stays under the job cap, so it's specifically the reusable-workflow cap.
    # Per consumer: caller + resolve + 1 leg = 3; base overhead is 4 (validate +
    # report-start + report-ci-failure + report-result).
    assert n_consumers * 3 + 4 < ORCHESTRATOR_MAX_TOTAL_JOBS
    with pytest.raises(SchemaError, match="reusable workflows"):
        render_orchestrator_workflow(by_pkg["a"], by_pkg, by_repo, closures, lane=EXECUTION_RUNNER)


def test_write_or_check_path_modes(tmp_path: Path) -> None:
    out = tmp_path / ".github/workflows/cross-repo-trigger.yml"
    # First write creates the parent directory too.
    assert _write_or_check_path(out, "hello\n", check=False) is True
    assert out.read_text() == "hello\n"
    # Idempotent
    assert _write_or_check_path(out, "hello\n", check=False) is False
    # Drift detected by --check
    assert _write_or_check_path(out, "different\n", check=True) is True
    # Check didn't actually write
    assert out.read_text() == "hello\n"
    # content=None deletes an existing workflow: the manifest is the source
    # of truth, so a kind that no longer opts into cross-repo dispatch must
    # not leave a dangling workflow behind.
    assert _write_or_check_path(out, None, check=False) is True
    assert not out.exists()
    # …and a second None call is a no-op once the file is gone.
    assert _write_or_check_path(out, None, check=False) is False


def test_local_sibling_layer_reads_clones_and_skips_missing(tmp_path: Path) -> None:
    """--sibling-root resolves owner/repo to <root>/<repo-name>/<manifest-path>.

    A sibling that is not checked out yields None, the same signal a missing remote
    manifest gives, so the BFS skips it rather than failing. That is what makes the
    flag safe to point at a partially-populated directory.
    """
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
    """The ref is deliberately ignored: the flag exists to read each clone's WORKING
    TREE, which is the state a coordinated cross-repo change lives in before it is
    pushed anywhere."""
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
    """An unresolved [[trigger-downstream]] target is the one silent failure here: it
    contributes no orchestrator job, so the fan-out shrinks and both --check and a
    plain run agree the smaller output is correct. Warn, naming the target and the
    --sibling-root escape hatch."""
    local = parse_manifest_text(_trigger_manifest("up", "ecmwf/up", ["ecmwf/absent"]), tmp_path / ".ci/manifest.toml")

    _fetch_sibling_manifests(local, None, ".ci/manifest.toml", sibling_root=tmp_path)

    err = capsys.readouterr().err
    assert "::warning::" in err
    assert "ecmwf/absent" in err
    assert "--sibling-root" in err


def test_no_warning_when_every_trigger_target_resolves(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The correct case must stay silent, or the warning becomes noise people filter out."""
    (tmp_path / "down" / ".ci").mkdir(parents=True)
    (tmp_path / "down" / ".ci" / "manifest.toml").write_text(_trigger_manifest("down", "ecmwf/down", []))
    local = parse_manifest_text(_trigger_manifest("up", "ecmwf/up", ["ecmwf/down"]), tmp_path / ".ci/manifest.toml")

    _fetch_sibling_manifests(local, None, ".ci/manifest.toml", sibling_root=tmp_path)

    assert "::warning::" not in capsys.readouterr().err
