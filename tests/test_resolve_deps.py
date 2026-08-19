# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for resolve_deps own-SHA resolution and the structured fields it emits.

A repo's own published artifact must be named after the branch-head sha — the
same value every consumer derives via resolve_ref_to_sha(repo, ref). On
pull_request events GITHUB_SHA is the synthetic merge commit, which no consumer
can resolve, so the resolver must NOT name the artifact after it. These tests
pin that: own-SHA always comes from the branch ref, never GITHUB_SHA, and a
missing branch fails loudly instead of minting a meaningless name.

They also pin that ResolvedDep.to_json carries the structured fields (platform /
compiler / build-type / python-version / deps-hash) explicitly, so the
dependency table renders them without re-parsing the artifact name.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack

import pytest

from ci_infrastructure import resolve_deps
from ci_infrastructure._github_api import canonical_option_segment
from ci_infrastructure.resolve_deps import (
    ArtifactName,
    DepSpec,
    Manifest,
    PackageName,
    PackageSpec,
    Ref,
    Repo,
    ResolvedDep,
    ResolvedOwn,
    ResolveError,
    Sha,
    _as_option,
    _parse_deps,
    _resolve_own_sha,
    make_artifact_name,
    producer_can_build,
    resolve_leg,
)

BRANCH_HEAD = "a" * 40
MERGE_COMMIT = "b" * 40


def test_own_sha_uses_branch_head_not_github_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a pull_request run: GITHUB_SHA is the merge commit, but the branch
    # head (what consumers resolve) is something else.
    monkeypatch.setenv("GITHUB_SHA", MERGE_COMMIT)
    calls: list[tuple[str, str]] = []

    def fake_resolve(repo: str, ref: str, token: str | None) -> Sha:
        calls.append((str(repo), str(ref)))
        return Sha(BRANCH_HEAD)

    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", fake_resolve)

    own_sha = _resolve_own_sha("owner/repo", "feature-sync-foo", token=None)

    assert own_sha == BRANCH_HEAD
    assert own_sha != MERGE_COMMIT
    assert calls == [("owner/repo", "feature-sync-foo")]


def test_own_sha_requires_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", MERGE_COMMIT)

    with pytest.raises(ResolveError, match="current-branch is required"):
        _resolve_own_sha("owner/repo", "", token=None)


class _DepOverrides(TypedDict, total=False):
    """The overridable fields of ResolvedDep, so `_dep(...)` kwargs are checked
    against each field's real type instead of a loose object/Any."""

    name: PackageName
    repo: Repo
    ref: Ref
    sha: Sha
    artifact_name: ArtifactName
    cached: bool
    source: Literal["artifact", "triggered rebuild"]
    needs_python: bool
    install_path: Path
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    deps_hash: str | None


def _dep(**overrides: Unpack[_DepOverrides]) -> ResolvedDep:
    base = ResolvedDep(
        name=PackageName("pkg"),
        repo=Repo("owner/pkg"),
        ref=Ref("main"),
        sha=Sha(BRANCH_HEAD),
        artifact_name=ArtifactName("pkg-..."),
        cached=True,
        source="artifact",
        needs_python=False,
        install_path=Path("/tmp/install/pkg"),
        platform="ubuntu-24.04",
        compiler="clang++-18",
        build_type="Release",
        python_version="3.11",
        deps_hash="abc12345",
    )
    return replace(base, **overrides)


def test_to_json_carries_structured_fields() -> None:
    j = _dep().to_json()
    assert j["platform"] == "ubuntu-24.04"
    assert j["compiler"] == "clang++-18"
    assert j["build-type"] == "Release"
    assert j["python-version"] == "3.11"
    assert j["deps-hash"] == "abc12345"
    assert j["ref"] == "main"


def test_to_json_blanks_absent_optionals() -> None:
    # A compiler-less, non-python leaf (the ecbuild shape): None segments become "".
    j = _dep(compiler=None, python_version=None, deps_hash=None).to_json()
    assert j["compiler"] == ""
    assert j["python-version"] == ""
    assert j["deps-hash"] == ""
    # build-type stays in its own field, never folded into platform.
    assert j["platform"] == "ubuntu-24.04"
    assert j["build-type"] == "Release"


# --- build-options axis -----------------------------------------------------

SHA40 = Sha("a" * 40)


def test_option_segment_canonical() -> None:
    assert canonical_option_segment("") == ""
    assert canonical_option_segment("stochastic-moments") == "opts.stochastic-moments"
    # A curated combo name is used verbatim.
    assert canonical_option_segment("moments-fast") == "opts.moments-fast"
    with pytest.raises(ValueError, match="only"):
        canonical_option_segment("bad+token")


def test_artifact_name_option_backward_compatible() -> None:
    # Empty option -> byte-identical to the pre-options name (no cache churn).
    without = make_artifact_name(PackageName("cxxmath"), SHA40, None, "ubuntu-24.04", "clang++-18", "Release", None)
    with_empty = make_artifact_name(
        PackageName("cxxmath"), SHA40, None, "ubuntu-24.04", "clang++-18", "Release", None, option=""
    )
    assert without == with_empty
    assert without == f"cxxmath-{SHA40}-ubuntu-24.04-clang++-18-Release"


def test_artifact_name_option_segment_appended() -> None:
    name = make_artifact_name(
        PackageName("cxxmath"),
        SHA40,
        None,
        "ubuntu-24.04",
        "clang++-18",
        "Release",
        None,
        option="stochastic-moments",
    )
    assert name == f"cxxmath-{SHA40}-ubuntu-24.04-clang++-18-Release-opts.stochastic-moments"


def _producer(*legs: dict[str, Any]) -> Manifest:
    return Manifest(
        package=PackageSpec(
            name="cxxmath",
            prefix=PackageName("cxxmath"),
            repo=Repo("o/cxx"),
            compiler_inputs=["cxx-compiler"],
        ),
        deps=[],
        matrix={"build": list(legs)},
    )


_BASE_LEG = {"cxx-compiler": "clang++-18", "build-type": "Release", "platform": "ubuntu-24.04"}


def test_producer_can_build_matches_requested_option() -> None:
    prod = _producer(_BASE_LEG, {**_BASE_LEG, "options": "stochastic-moments"})
    assert producer_can_build(prod, {**_BASE_LEG, "options": ""})
    assert producer_can_build(prod, {**_BASE_LEG, "options": "stochastic-moments"})
    # A config no producer leg offers cannot be satisfied.
    assert not producer_can_build(prod, {**_BASE_LEG, "options": "fastmath"})


def test_producer_plain_leg_cannot_satisfy_moments() -> None:
    # A leg WITHOUT options means the empty config (a concrete value), not a
    # wildcard: it must not satisfy a moments request.
    prod = _producer(_BASE_LEG)
    assert producer_can_build(prod, {**_BASE_LEG, "options": ""})
    assert not producer_can_build(prod, {**_BASE_LEG, "options": "stochastic-moments"})


def test_parse_deps_option_literal_and_input() -> None:
    literal = _parse_deps(
        {
            "deps": [
                {
                    "repo": "o/x",
                    "package": "x",
                    "ref": "main",
                    "compiler-inputs": ["cxx-compiler"],
                    "options": "stochastic-moments",
                }
            ]
        }
    )[0]
    assert literal.option == "stochastic-moments"
    assert literal.options_input is None

    per_leg = _parse_deps(
        {
            "deps": [
                {
                    "repo": "o/x",
                    "package": "x",
                    "ref": "main",
                    "compiler-inputs": ["cxx-compiler"],
                    "options-input": "x-options",
                }
            ]
        }
    )[0]
    assert per_leg.option == ""
    assert per_leg.options_input == "x-options"


def test_as_option_rejects_bad_token() -> None:
    with pytest.raises(ResolveError, match="invalid build option"):
        _as_option("bad+token", context="test")


def test_as_option_rejects_list() -> None:
    # The composable-list form was replaced by scalar named configs.
    with pytest.raises(ResolveError, match="scalar config name"):
        _as_option(["stochastic-moments"], context="test")


def test_options_do_not_propagate_and_ripple_via_deps_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumer that selects a moments upstream gets a different upstream
    artifact name (opts segment) AND a different OWN name (via deps-hash), while
    the upstream dep itself is consumed with exactly the requested options."""
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha("c" * 40))
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: True)

    own = PackageSpec(
        name="cxxmath-python",
        prefix=PackageName("cxxmath-python"),
        repo=Repo("o/cxxpy"),
        compiler_inputs=["cxx-compiler"],
    )
    dep = DepSpec(
        repo=Repo("o/cxx"),
        package=PackageName("cxxmath"),
        ref=Ref("main"),
        compiler_inputs=["cxx-compiler"],
        build_type_input="build-type",
        platform_input="platform",
        needs_python=False,
        python_version_input="python-version",
        options_input="cxxmath-options",
    )
    # Leg fields are all scalar strings (discriminators + the scalar option config).
    leg: dict[str, str] = {
        "cxx-compiler": "clang++-18",
        "build-type": "Release",
        "platform": "ubuntu-24.04",
        "python-version": "3.12",
    }

    def run(matrix_entry: dict[str, str]) -> tuple[list[ResolvedDep], ResolvedOwn]:
        return resolve_leg(
            own=own,
            own_deps=[dep],
            own_sha=Sha("d" * 40),
            matrix_entry=matrix_entry,
            manifest_cache={},
            sync_branch=None,
            sync_exists_by_repo={},
            sha_cache={},
            artifact_cache={},
            run_state_cache={},
            token=None,
            can_dispatch=False,
            dispatch_plans={},
        )

    plain_deps, plain_own = run(leg)
    moments_deps, moments_own = run({**leg, "cxxmath-options": "stochastic-moments"})

    # The upstream dep is consumed plain vs moments per the leg selection.
    assert plain_deps[0].artifact_name.endswith("-Release")
    assert moments_deps[0].artifact_name.endswith("-Release-opts.stochastic-moments")
    # The consumer's OWN name carries no opts segment (own options empty)...
    assert moments_own.artifact_name.endswith("-Release")
    # ...yet differs between the two legs because the deps-hash rolls up the
    # (different) consumed cxxmath name.
    assert plain_own.artifact_name != moments_own.artifact_name


def test_parse_deps_when_predicate() -> None:
    """`when` accepts a scalar or a list, and defaults to None (applies to every leg)."""
    base = {"repo": "o/x", "package": "x", "ref": "main", "compiler-inputs": ["cxx-compiler"]}

    assert _parse_deps({"deps": [base]})[0].when is None

    listed = _parse_deps({"deps": [{**base, "when": {"options": ["extended", "full"]}}]})[0]
    assert listed.when == {"options": frozenset({"extended", "full"})}

    # A bare scalar is sugar for a one-element list.
    scalar = _parse_deps({"deps": [{**base, "when": {"build-type": "Debug"}}]})[0]
    assert scalar.when == {"build-type": frozenset({"Debug"})}

    # Multiple keys must ALL match; values compare as strings.
    multi = _parse_deps({"deps": [{**base, "when": {"platform": "ubuntu-24.04", "python-version": 3.12}}]})[0]
    assert multi.when == {"platform": frozenset({"ubuntu-24.04"}), "python-version": frozenset({"3.12"})}


@pytest.mark.parametrize("bad", [{}, [], "options", {"options": []}, {"options": [["nested"]]}])
def test_parse_deps_when_rejects_bad_shape(bad: Any) -> None:
    """A malformed predicate fails loudly: silently ignoring it would put the dep
    back into every leg's artifact identity, surfacing only as surprise rebuilds."""
    base = {"repo": "o/x", "package": "x", "ref": "main", "compiler-inputs": ["cxx-compiler"]}
    with pytest.raises(ValueError, match="when"):
        _parse_deps({"deps": [{**base, "when": bad}]})


def test_applies_to_requires_every_key_and_ignores_missing_fields() -> None:
    spec = DepSpec(
        repo=Repo("o/x"),
        package=PackageName("x"),
        ref=Ref("main"),
        compiler_inputs=["cxx-compiler"],
        build_type_input="build-type",
        platform_input="platform",
        needs_python=False,
        python_version_input="python-version",
        when={"options": frozenset({"extended"}), "build-type": frozenset({"Release"})},
    )
    assert spec.applies_to({"options": "extended", "build-type": "Release"})
    assert not spec.applies_to({"options": "extended", "build-type": "Debug"})
    # A leg that simply omits the field must NOT match — this is what keeps a
    # predicate off the plain legs, which carry no such key at all.
    assert not spec.applies_to({"build-type": "Release"})


def test_when_scopes_dep_out_of_identity_of_nonmatching_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scoped-out dep is absent from cmake-prefix-path AND from deps-hash8.

    The second half is the point of the feature: adding a dep that only one option
    config needs must not change the artifact name — and so must not force a
    rebuild — of the legs that never link against it.
    """
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha("c" * 40))
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: True)

    own = PackageSpec(
        name="consumer",
        prefix=PackageName("consumer"),
        repo=Repo("o/consumer"),
        compiler_inputs=["cxx-compiler"],
    )
    always = DepSpec(
        repo=Repo("o/base"),
        package=PackageName("base"),
        ref=Ref("main"),
        compiler_inputs=[],
        build_type_input="build-type",
        platform_input="platform",
        needs_python=False,
        python_version_input="python-version",
    )
    scoped = replace(
        always,
        repo=Repo("o/extra"),
        package=PackageName("extra"),
        compiler_inputs=["cxx-compiler"],
        when={"options": frozenset({"extended"})},
    )
    leg: dict[str, str] = {
        "cxx-compiler": "clang++-18",
        "build-type": "Release",
        "platform": "ubuntu-24.04",
    }

    def run(own_deps: list[DepSpec], matrix_entry: dict[str, str]) -> tuple[list[ResolvedDep], ResolvedOwn]:
        return resolve_leg(
            own=own,
            own_deps=own_deps,
            own_sha=Sha("d" * 40),
            matrix_entry=matrix_entry,
            manifest_cache={},
            sync_branch=None,
            sync_exists_by_repo={},
            sha_cache={},
            artifact_cache={},
            run_state_cache={},
            token=None,
            can_dispatch=False,
            dispatch_plans={},
        )

    plain_deps, plain_own = run([always, scoped], leg)
    ext_deps, ext_own = run([always, scoped], {**leg, "options": "extended"})

    # The scoped dep reaches only the matching leg.
    assert [d.name for d in plain_deps] == ["base"]
    assert sorted(d.name for d in ext_deps) == ["base", "extra"]

    # ...and only that leg's identity moves.
    assert plain_own.deps_hash != ext_own.deps_hash

    # The decisive check: the plain leg's OWN name is byte-identical to what it
    # would be if the scoped dep had never been declared at all.
    _, without_scoped = run([always], leg)
    assert plain_own.artifact_name == without_scoped.artifact_name
    assert plain_own.deps_hash == without_scoped.deps_hash
