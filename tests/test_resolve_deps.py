# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""resolve_deps: own SHA, structured fields, build options, `when`, ctest and lane dispatch."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Final

import pytest

from ci_infrastructure import resolve_deps
from ci_infrastructure._github_api import (
    EXECUTION_HPC,
    EXECUTION_RUNNER,
    Execution,
    WorkflowRuns,
    canonical_option_segment,
)
from ci_infrastructure.manifest import DepTable
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
    _resolve_own_sha,
    _to_dep_spec,
    bfs_load_manifests,
    make_artifact_name,
    parse_manifest,
    parse_pin,
    producer_can_build,
    resolve_leg,
)


def _parse_deps(data: dict[str, Any]) -> list[DepSpec]:
    return [_to_dep_spec(DepTable.model_validate(d)) for d in data["deps"]]


BRANCH_HEAD: Final = "a" * 40
MERGE_COMMIT: Final = "b" * 40


def test_own_sha_requires_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", MERGE_COMMIT)

    with pytest.raises(ResolveError, match="current-branch is required"):
        _resolve_own_sha("owner/repo", "", token=None)


def _dep(**overrides: Any) -> ResolvedDep:
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


def test_to_json_carries_structured_fields_and_blanks_absent_optionals() -> None:
    j = _dep().to_json()
    assert (j["platform"], j["compiler"], j["build-type"], j["ref"]) == (
        "ubuntu-24.04",
        "clang++-18",
        "Release",
        "main",
    )
    assert (j["python-version"], j["deps-hash"]) == ("3.11", "abc12345")

    j = _dep(compiler=None, python_version=None, deps_hash=None).to_json()
    assert (j["compiler"], j["python-version"], j["deps-hash"]) == ("", "", "")
    assert (j["platform"], j["build-type"]) == ("ubuntu-24.04", "Release")


SHA40: Final = Sha(BRANCH_HEAD)


def test_option_segment_canonical() -> None:
    assert canonical_option_segment("") == ""
    assert canonical_option_segment("stochastic-moments") == "opts.stochastic-moments"
    assert canonical_option_segment("moments-fast") == "opts.moments-fast"
    with pytest.raises(ValueError, match="only"):
        canonical_option_segment("bad+token")


def test_artifact_name_option_segment() -> None:
    args = (PackageName("cxxmath"), SHA40, None, "ubuntu-24.04", "clang++-18", "Release", None)
    plain = f"cxxmath-{SHA40}-ubuntu-24.04-clang++-18-Release"
    assert make_artifact_name(*args) == make_artifact_name(*args, option="") == plain
    assert make_artifact_name(*args, option="stochastic-moments") == f"{plain}-opts.stochastic-moments"


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


_LEG: Final = {"cxx-compiler": "clang++-18", "build-type": "Release", "platform": "ubuntu-24.04"}


def test_producer_can_build_matches_requested_option() -> None:
    prod = _producer(_LEG, {**_LEG, "options": "stochastic-moments"})
    assert producer_can_build(prod, {**_LEG, "options": ""})
    assert producer_can_build(prod, {**_LEG, "options": "stochastic-moments"})
    assert not producer_can_build(prod, {**_LEG, "options": "fastmath"})


def test_producer_plain_leg_cannot_satisfy_moments() -> None:
    prod = _producer(_LEG)
    assert producer_can_build(prod, {**_LEG, "options": ""})
    assert not producer_can_build(prod, {**_LEG, "options": "stochastic-moments"})


_DEP_BASE: Final = {"repo": "o/x", "package": "x", "ref": "main", "compiler-inputs": ["cxx-compiler"]}


def test_parse_deps_option_literal_and_input() -> None:
    [literal] = _parse_deps({"deps": [{**_DEP_BASE, "options": "stochastic-moments"}]})
    assert (literal.option, literal.options_input) == ("stochastic-moments", None)

    [per_leg] = _parse_deps({"deps": [{**_DEP_BASE, "options-input": "x-options"}]})
    assert (per_leg.option, per_leg.options_input) == ("", "x-options")


@pytest.mark.parametrize(("value", "match"), [("bad+token", "invalid build option"), (["x"], "scalar config name")])
def test_as_option_rejects(value: Any, match: str) -> None:
    with pytest.raises(ResolveError, match=match):
        _as_option(value, context="test")


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
    own: PackageSpec,
    deps: list[DepSpec],
    leg: dict[str, str],
    lane: Execution = EXECUTION_RUNNER,
    manifest_cache: dict[tuple[Repo, Ref], Manifest] | None = None,
    pins: dict[Repo, Ref] | None = None,
) -> tuple[list[ResolvedDep], ResolvedOwn]:
    return resolve_leg(
        own=own,
        own_deps=deps,
        own_sha=Sha("d" * 40),
        matrix_entry=leg,
        manifest_cache=manifest_cache or {},
        sync_branch=None,
        sync_exists_by_repo={},
        sha_cache={},
        artifact_cache={},
        run_state_cache={},
        token=None,
        can_dispatch=False,
        lane=lane,
        dispatch_plans={},
        pins=pins,
    )


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha("c" * 40))
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: True)


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


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (None, None),
        ({"options": ["extended", "full"]}, {"options": frozenset({"extended", "full"})}),
        ({"build-type": "Debug"}, {"build-type": frozenset({"Debug"})}),
        (
            {"platform": "ubuntu-24.04", "python-version": 3.12},
            {"platform": frozenset({"ubuntu-24.04"}), "python-version": frozenset({"3.12"})},
        ),
    ],
)
def test_parse_deps_when_predicate(when: Any, expected: Any) -> None:
    dep = _DEP_BASE if when is None else {**_DEP_BASE, "when": when}
    assert _parse_deps({"deps": [dep]})[0].when == expected


@pytest.mark.parametrize("bad", [{}, [], "options", {"options": []}, {"options": [["nested"]]}])
def test_parse_deps_when_rejects_bad_shape(bad: Any) -> None:
    with pytest.raises(ValueError, match="when"):
        _parse_deps({"deps": [{**_DEP_BASE, "when": bad}]})


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


def test_ctest_parsed_per_kind_verbatim_and_not_inherited_through_reuse_matrix() -> None:
    kinds = resolve_deps.parse_manifest(_CTEST_MANIFEST).ctest_by_kind

    assert kinds["build"] == resolve_deps.CtestSpec(enabled=True, args="-L nightly -E 's_test|s_zombies' -j 8")
    assert kinds["build-hpc"] == resolve_deps.CtestSpec(enabled=False, args="")
    assert kinds["test"] == resolve_deps.CtestSpec(enabled=True, args='-j "$(nproc)"')


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

    with pytest.raises(ValueError, match=r"\[matrix\.build\]\.ctest Input should be a valid boolean"):
        resolve_deps.parse_manifest(manifest('ctest = "yes"'))

    with pytest.raises(ValueError, match=r"\[matrix\.build\]\.ctest-args Input should be a valid string"):
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


_PINNED: Final = Ref("a" * 40)
_MIDDLE_MANIFEST: Final = """
[package]
name = "middle"
prefix = "middle"
repo = "o/middle"
compiler-inputs = ["cxx-compiler"]

[[deps]]
repo = "o/base"
package = "base"
ref = "main"
compiler-inputs = []
"""


@pytest.mark.usefixtures("offline")
def test_pin_resolves_the_change_under_test_directly_and_through_a_middle_package() -> None:
    """Downstream CI builds against the commit under test, not the pinned repo's default branch."""
    own = _own("top")
    base, middle = _dep_spec("base", compiler_inputs=[]), _dep_spec("middle")
    cache = {(Repo("o/middle"), Ref("main")): parse_manifest(_MIDDLE_MANIFEST)}

    unpinned, _ = _resolve(own, [base, middle], dict(_LEG), manifest_cache=cache)
    pinned, _ = _resolve(own, [base, middle], dict(_LEG), manifest_cache=cache, pins={Repo("o/base"): _PINNED})

    by_name = {str(d.name): d for d in pinned}
    assert by_name["base"].sha == Sha(_PINNED)
    assert by_name["middle"].sha == "c" * 40
    assert by_name["middle"].deps_hash != {str(d.name): d for d in unpinned}["middle"].deps_hash


def test_pinned_commit_without_its_artifact_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: False)
    monkeypatch.setattr(resolve_deps, "probe_workflow_runs", lambda repo, sha, token: WorkflowRuns(state="none"))
    with pytest.raises(ResolveError, match="the commit under test"):
        _resolve(_own("top"), [_dep_spec("base")], dict(_LEG), pins={Repo("o/base"): _PINNED})


def test_bfs_reads_pinned_manifests_at_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[tuple[str, str]] = []

    def layer(
        repos_refs: Any, sync_branch: Any, token: Any, path: Any
    ) -> dict[tuple[str, str], tuple[str | None, bool]]:
        requested.extend(repos_refs)
        return {rr: (_MIDDLE_MANIFEST if rr[0] == "o/middle" else None, False) for rr in repos_refs}

    monkeypatch.setattr(resolve_deps, "fetch_manifests_layer", layer)
    bfs_load_manifests([_dep_spec("middle")], None, None, ".ci/manifest.toml", pins={Repo("o/base"): _PINNED})
    assert requested == [("o/middle", "main"), ("o/base", _PINNED)]


@pytest.mark.parametrize(
    ("pin", "expected"),
    [("", {}), (f"o/base@{'a' * 40}", {Repo("o/base"): Ref("a" * 40)})],
)
def test_parse_pin(pin: str, expected: dict[Repo, Ref]) -> None:
    assert parse_pin(pin) == expected


@pytest.mark.parametrize("pin", ["o/base", "o/base@main", f"base@{'a' * 40}"])
def test_parse_pin_rejects(pin: str) -> None:
    with pytest.raises((ResolveError, ValueError)):
        parse_pin(pin)


def _middle_declaring_base(**base: str) -> dict[tuple[Repo, Ref], Manifest]:
    decl = "".join(f"{k} = {v}\n" for k, v in {"ref": '"main"', "compiler-inputs": "[]", **base}.items())
    text = _MIDDLE_MANIFEST.replace('ref = "main"\ncompiler-inputs = []\n', decl)
    return {(Repo("o/middle"), Ref("main")): parse_manifest(text)}


@pytest.mark.usefixtures("offline")
@pytest.mark.parametrize("base_first", [True, False])
def test_conflicting_declarations_of_a_dep_fail_in_either_order(base_first: bool) -> None:
    base, middle = _dep_spec("base", ref=Ref("develop"), compiler_inputs=[]), _dep_spec("middle")
    deps = [base, middle] if base_first else [middle, base]
    with pytest.raises(
        ResolveError, match=r"'base' is declared differently .*ref 'develop' vs 'main'|ref 'main' vs 'develop'"
    ):
        _resolve(_own("top"), deps, dict(_LEG), manifest_cache=_middle_declaring_base())


@pytest.mark.usefixtures("offline")
def test_differing_compiler_inputs_fail() -> None:
    base, middle = _dep_spec("base"), _dep_spec("middle")
    with pytest.raises(ResolveError, match="compiler"):
        _resolve(_own("top"), [base, middle], dict(_LEG), manifest_cache=_middle_declaring_base())


def test_one_branch_agrees_while_its_commit_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declarations compare refs, not SHAs, so a push between lookups is not a conflict."""
    commits = iter("abcdef")
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha(next(commits) * 40))
    monkeypatch.setattr("ci_infrastructure.s3_store.object_exists", lambda name: True)
    base, middle = _dep_spec("base", compiler_inputs=[]), _dep_spec("middle")
    deps, _ = _resolve(_own("top"), [base, middle], dict(_LEG), manifest_cache=_middle_declaring_base())
    assert [d.name for d in deps] == ["base", "middle"]
