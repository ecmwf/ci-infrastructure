# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""resolve_deps: own SHA, structured fields, build options, `when`, ctest and lane dispatch."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Final, Literal, TypedDict, Unpack

import pytest

from ci_infrastructure import resolve_deps
from ci_infrastructure._github_api import (
    EXECUTION_HPC,
    EXECUTION_RUNNER,
    Execution,
    canonical_option_segment,
)
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

BRANCH_HEAD: Final = "a" * 40
MERGE_COMMIT: Final = "b" * 40


def test_own_sha_requires_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", MERGE_COMMIT)

    with pytest.raises(ResolveError, match="current-branch is required"):
        _resolve_own_sha("owner/repo", "", token=None)


class _DepOverrides(TypedDict, total=False):
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
    j = _dep(compiler=None, python_version=None, deps_hash=None).to_json()
    assert j["compiler"] == ""
    assert j["python-version"] == ""
    assert j["deps-hash"] == ""
    assert j["platform"] == "ubuntu-24.04"
    assert j["build-type"] == "Release"


# --- build-options axis -----------------------------------------------------

SHA40: Final = Sha("a" * 40)


def test_option_segment_canonical() -> None:
    assert canonical_option_segment("") == ""
    assert canonical_option_segment("stochastic-moments") == "opts.stochastic-moments"
    assert canonical_option_segment("moments-fast") == "opts.moments-fast"
    with pytest.raises(ValueError, match="only"):
        canonical_option_segment("bad+token")


def test_empty_option_adds_no_segment() -> None:
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


_BASE_LEG: Final = {"cxx-compiler": "clang++-18", "build-type": "Release", "platform": "ubuntu-24.04"}


def test_producer_can_build_matches_requested_option() -> None:
    prod = _producer(_BASE_LEG, {**_BASE_LEG, "options": "stochastic-moments"})
    assert producer_can_build(prod, {**_BASE_LEG, "options": ""})
    assert producer_can_build(prod, {**_BASE_LEG, "options": "stochastic-moments"})
    assert not producer_can_build(prod, {**_BASE_LEG, "options": "fastmath"})


def test_producer_plain_leg_cannot_satisfy_moments() -> None:
    # No options is the empty config, not a wildcard.
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
    with pytest.raises(ResolveError, match="scalar config name"):
        _as_option(["stochastic-moments"], context="test")


def _own(name: str) -> PackageSpec:
    return PackageSpec(name=name, prefix=PackageName(name), repo=Repo(f"o/{name}"), compiler_inputs=["cxx-compiler"])


def _dep_spec(package: str, **overrides: Any) -> DepSpec:
    base = DepSpec(
        repo=Repo(f"o/{package}"),
        package=PackageName(package),
        ref=Ref("main"),
        compiler_inputs=["cxx-compiler"],
        build_type_input="build-type",
        platform_input="platform",
        needs_python=False,
        python_version_input="python-version",
    )
    return replace(base, **overrides)


def _resolve(
    own: PackageSpec, deps: list[DepSpec], leg: dict[str, str], lane: Execution = EXECUTION_RUNNER
) -> tuple[list[ResolvedDep], ResolvedOwn]:
    return resolve_leg(
        own=own,
        own_deps=deps,
        own_sha=Sha("d" * 40),
        matrix_entry=leg,
        manifest_cache={},
        sync_branch=None,
        sync_exists_by_repo={},
        sha_cache={},
        artifact_cache={},
        run_state_cache={},
        token=None,
        can_dispatch=False,
        lane=lane,
        dispatch_plans={},
    )


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha("c" * 40))
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: True)


_LEG: Final = {"cxx-compiler": "clang++-18", "build-type": "Release", "platform": "ubuntu-24.04"}


@pytest.mark.usefixtures("offline")
def test_options_do_not_propagate_and_ripple_via_deps_hash() -> None:
    """An upstream option renames the dep and, via deps-hash, us; we get no opts segment."""
    own = _own("cxxmath-python")
    dep = _dep_spec("cxxmath", options_input="cxxmath-options")
    leg = {**_LEG, "python-version": "3.12"}

    plain_deps, plain_own = _resolve(own, [dep], leg)
    moments_deps, moments_own = _resolve(own, [dep], {**leg, "cxxmath-options": "stochastic-moments"})

    assert plain_deps[0].artifact_name.endswith("-Release")
    assert moments_deps[0].artifact_name.endswith("-Release-opts.stochastic-moments")
    assert moments_own.artifact_name.endswith("-Release")
    assert plain_own.artifact_name != moments_own.artifact_name


@pytest.mark.usefixtures("offline")
@pytest.mark.parametrize(("lane", "tail"), [(EXECUTION_HPC, "-Release-hpcv3"), (EXECUTION_RUNNER, "-Release")])
def test_template_version_marks_hpc_lane_names_only(
    monkeypatch: pytest.MonkeyPatch, lane: Execution, tail: str
) -> None:
    monkeypatch.setattr("ci_infrastructure._github_api.HPC_TEMPLATE_VERSION", 3)
    leg = {"cxx-compiler": "g++-8", "build-type": "Release", "platform": "hpc-atos-gnu"}
    deps, resolved_own = _resolve(_own("cxxmath-python"), [_dep_spec("cxxmath")], leg, lane)

    assert deps[0].artifact_name.endswith(tail)
    assert resolved_own.artifact_name.endswith(tail)


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
    base = {"repo": "o/x", "package": "x", "ref": "main", "compiler-inputs": ["cxx-compiler"]}
    with pytest.raises(ValueError, match="when"):
        _parse_deps({"deps": [{**base, "when": bad}]})


def test_applies_to_requires_every_key_and_ignores_missing_fields() -> None:
    spec = _dep_spec("x", when={"options": frozenset({"extended"}), "build-type": frozenset({"Release"})})
    assert spec.applies_to({"options": "extended", "build-type": "Release"})
    assert not spec.applies_to({"options": "extended", "build-type": "Debug"})
    assert not spec.applies_to({"build-type": "Release"})


@pytest.mark.usefixtures("offline")
def test_when_scopes_dep_out_of_identity_of_nonmatching_legs() -> None:
    """A scoped-out dep is absent from the prefix path and from deps-hash8, so other legs do not rebuild."""
    own = _own("consumer")
    always = _dep_spec("base", compiler_inputs=[])
    scoped = _dep_spec("extra", when={"options": frozenset({"extended"})})

    plain_deps, plain_own = _resolve(own, [always, scoped], dict(_LEG))
    ext_deps, ext_own = _resolve(own, [always, scoped], {**_LEG, "options": "extended"})

    assert [d.name for d in plain_deps] == ["base"]
    assert sorted(d.name for d in ext_deps) == ["base", "extra"]
    assert plain_own.deps_hash != ext_own.deps_hash

    _, without_scoped = _resolve(own, [always], dict(_LEG))
    assert plain_own.artifact_name == without_scoped.artifact_name
    assert plain_own.deps_hash == without_scoped.deps_hash


# --- [matrix.<kind>] ctest / ctest-args: read by hand-written ci.yml via matrix._resolved ---

_CTEST_MANIFEST: Final = """
[package]
name = "x"
prefix = "x"
repo = "o/x"
compiler-inputs = ["cxx-compiler"]

[[matrix.build.include]]
cxx-compiler = "g++-13"
platform = "ubuntu-24.04"

[matrix.build]
ctest = true
ctest-args = "-L nightly -E 's_test|s_zombies' -j 8"

[[matrix.build-hpc.include]]
cxx-compiler = "g++-13"
platform = "hpc-atos-gnu"

[matrix.build-hpc]
execution = "hpc"

[matrix.test]
reuse-matrix = "build"
ctest = true
ctest-args = '-j "$(nproc)"'
"""


def test_ctest_parsed_per_kind() -> None:
    kinds = resolve_deps.parse_manifest(_CTEST_MANIFEST).ctest_by_kind

    assert kinds["build"] == resolve_deps.CtestSpec(enabled=True, args="-L nightly -E 's_test|s_zombies' -j 8")
    # HPC job-scripts run ctest themselves.
    assert kinds["build-hpc"] == resolve_deps.CtestSpec(enabled=False, args="")


def test_ctest_is_per_kind_not_inherited_through_reuse_matrix() -> None:
    kinds = resolve_deps.parse_manifest(_CTEST_MANIFEST).ctest_by_kind

    assert kinds["test"].args == '-j "$(nproc)"'
    assert kinds["test"].args != kinds["build"].args


def test_ctest_args_survive_shell_metacharacters_verbatim() -> None:
    kinds = resolve_deps.parse_manifest(_CTEST_MANIFEST).ctest_by_kind

    assert "'s_test|s_zombies'" in kinds["build"].args
    assert '"$(nproc)"' in kinds["test"].args


def test_ctest_rejects_wrong_types() -> None:
    def manifest(block: str) -> str:
        return f"""
[package]
name = "x"
prefix = "x"
repo = "o/x"
compiler-inputs = []

[[matrix.build.include]]
platform = "ubuntu-24.04"

[matrix.build]
{block}
"""

    with pytest.raises(ValueError, match=r"\[matrix\.build\]\.ctest must be a boolean"):
        resolve_deps.parse_manifest(manifest('ctest = "yes"'))

    with pytest.raises(ValueError, match=r"\[matrix\.build\]\.ctest-args must be a string"):
        resolve_deps.parse_manifest(manifest("ctest = true\nctest-args = 8"))


def test_dispatch_plans_are_keyed_by_lane_not_just_repo_and_ref() -> None:
    """A producer missing its artifact on both lanes needs two dispatches."""
    plans: dict[tuple[Repo, Ref, Execution], resolve_deps.DispatchPlan] = {}
    spec = _dep_spec("up")
    for lane in (EXECUTION_RUNNER, EXECUTION_HPC):
        resolve_deps._classify_orphan_pin(
            spec=spec,
            ref=Ref("main"),
            sha=Sha(BRANCH_HEAD),
            artifact_name=ArtifactName(f"up-{BRANCH_HEAD}-{lane}"),
            matrix_entry={},
            manifest_cache={},
            sync_branch=None,
            sync_exists_by_repo={},
            can_dispatch=True,
            lane=lane,
            dispatch_plans=plans,
        )

    assert sorted(p.lane for p in plans.values()) == ["hpc", "runner"]
