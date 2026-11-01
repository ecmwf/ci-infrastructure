# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""`allow-stale`: a pull request uses a dep's artifact built against older upstreams instead of waiting."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from ci_infrastructure import resolve_deps, s3_store
from ci_infrastructure._github_api import EXECUTION_RUNNER, WorkflowRuns
from ci_infrastructure.manifest import DepTable
from ci_infrastructure.resolve_deps import (
    DepSpec,
    DispatchPlan,
    PackageName,
    PackageSpec,
    Ref,
    Repo,
    ResolvedDep,
    Sha,
    StaleFallback,
    parse_manifest,
    resolve_leg,
)

_LEG: Final = {"cxx-compiler": "clang++-18", "build-type": "Release", "platform": "ubuntu-24.04"}
_OLD, _NEW, _MIDDLE, _UNMERGED = Sha("0" * 40), Sha("1" * 40), Sha("m" * 40), Sha("a" * 40)
_MIDDLE_MANIFEST: Final = """
[package]
visibility = "private"
name = "middle"
repo = "o/middle"
compiler-inputs = ["cxx-compiler"]

[[deps]]
repo = "o/base"
package = "base"
ref = "main"
compiler-inputs = []

[[matrix.build.include]]
cxx-compiler = "clang++-18"
build-type = "Release"
platform = "ubuntu-24.04"
"""


@dataclass
class _Store(s3_store.ArtifactIndex):
    """The artifacts in upload order, and every sidecar the resolver records."""

    tars: list[str] = field(default_factory=list)
    sidecars: dict[str, Any] = field(default_factory=dict)

    def exists(self, name: str, prefix: str) -> bool:
        return name in self.tars

    def with_sidecar(self, prefix: str) -> list[str]:
        return [n for n in reversed(self.tars) if n.startswith(prefix) and n in self.sidecars]

    def read_sidecar(self, name: str) -> Any:
        return json.loads(json.dumps(self.sidecars[name]))

    def record_sidecar(self, name: str, prefix: str, payload: Mapping[str, Any]) -> None:
        self.sidecars.setdefault(name, payload)


def _spec(package: str, **overrides: Any) -> DepSpec:
    base = DepSpec(
        repo=Repo(f"o/{package}"),
        package=PackageName(package),
        ref=Ref("main"),
        compiler_inputs=[] if package == "base" else ["cxx-compiler"],
        build_type_input="build-type",
        platform_input="platform",
    )
    return replace(base, **overrides)


@pytest.fixture
def base_sha(monkeypatch: pytest.MonkeyPatch) -> list[Sha]:
    """Where o/base's main is; the test moves it."""
    head = [_OLD]
    monkeypatch.setattr(
        resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: head[0] if repo == "o/base" else _MIDDLE
    )
    monkeypatch.setattr(resolve_deps, "probe_workflow_runs", lambda repo, sha, token: WorkflowRuns(state="none"))
    monkeypatch.setattr(resolve_deps, "latest_ci_run", lambda repo, sha, token: None)
    monkeypatch.setattr(resolve_deps, "is_ancestor", lambda repo, older, head, token: older != _UNMERGED)
    return head


def _resolve(
    store: _Store,
    deps: list[DepSpec],
    *,
    allow_stale: bool,
    plans: dict[Any, DispatchPlan] | None = None,
    pins: dict[Repo, Ref] | None = None,
    notes: set[str] | None = None,
) -> dict[str, ResolvedDep]:
    stale = StaleFallback(token=None, notes=set() if notes is None else notes) if allow_stale else None
    resolved, _ = resolve_leg(
        own=PackageSpec(name="top", prefix=PackageName("top"), repo=Repo("o/top"), compiler_inputs=["cxx-compiler"]),
        own_deps=deps,
        own_sha=Sha("d" * 40),
        matrix_entry=dict(_LEG),
        manifest_cache={(Repo("o/middle"), Ref("main")): parse_manifest(_MIDDLE_MANIFEST)},
        sync_branch=None,
        sync_exists_by_repo={},
        sha_cache={},
        artifact_index=store,
        run_state_cache={},
        token=None,
        can_dispatch=True,
        lane=EXECUTION_RUNNER,
        dispatch_plans={} if plans is None else plans,
        pins=pins,
        stale=stale,
    )
    return {str(d.name): d for d in resolved}


def _published_then_base_moves(base_sha: list[Sha], store: _Store, deps: list[DepSpec]) -> dict[str, ResolvedDep]:
    """middle and base built at the old base; base's new commit is built, middle's rebuild is not."""
    old = _resolve(store, deps, allow_stale=False)
    store.tars += [old["base"].artifact_name, old["middle"].artifact_name]
    base_sha[0] = _NEW
    store.tars.append(_resolve(store, deps, allow_stale=False)["base"].artifact_name)
    return old


def test_a_missing_dep_falls_back_to_its_build_against_the_older_upstream(base_sha: list[Sha]) -> None:
    store = _Store()
    deps = [_spec("middle", allow_stale=True)]
    old = _published_then_base_moves(base_sha, store, deps)

    plans: dict[Any, DispatchPlan] = {}
    notes: set[str] = set()
    got = _resolve(store, deps, allow_stale=True, plans=plans, notes=notes)

    assert got["middle"].artifact_name == old["middle"].artifact_name
    assert (got["middle"].source, got["middle"].cached) == ("stale artifact", True)
    assert got["base"].artifact_name == old["base"].artifact_name
    assert got["base"].source == "stale artifact"
    assert plans == {}
    [note] = notes
    assert "o/base@00000000 (main is at 11111111)" in note


def test_without_the_fallback_the_dep_is_rebuilt(base_sha: list[Sha]) -> None:
    store = _Store()
    deps = [_spec("middle", allow_stale=True)]
    _published_then_base_moves(base_sha, store, deps)

    plans: dict[Any, DispatchPlan] = {}
    got = _resolve(store, deps, allow_stale=False, plans=plans)

    assert (got["middle"].source, got["base"].sha) == ("triggered rebuild", _NEW)
    assert [p.repo for p in plans.values()] == ["o/middle"]


def test_a_dep_without_allow_stale_is_rebuilt(base_sha: list[Sha]) -> None:
    store = _Store()
    deps = [_spec("middle")]
    _published_then_base_moves(base_sha, store, deps)
    assert _resolve(store, deps, allow_stale=True)["middle"].source == "triggered rebuild"


@pytest.mark.parametrize("base_allow_stale", [False, True])
def test_a_listed_dep_is_never_aged(base_sha: list[Sha], base_allow_stale: bool) -> None:
    """top also lists base: the old base under middle would replace it, so middle is rebuilt."""
    store = _Store()
    deps = [_spec("middle", allow_stale=True), _spec("base", allow_stale=base_allow_stale)]
    _published_then_base_moves(base_sha, store, deps)

    got = _resolve(store, deps, allow_stale=True)
    assert (got["middle"].source, got["base"].sha) == ("triggered rebuild", _NEW)


def test_no_fallback_once_the_older_upstream_is_evicted(base_sha: list[Sha]) -> None:
    store = _Store()
    deps = [_spec("middle", allow_stale=True)]
    old = _published_then_base_moves(base_sha, store, deps)
    store.tars.remove(old["base"].artifact_name)
    assert _resolve(store, deps, allow_stale=True)["middle"].source == "triggered rebuild"


def test_no_fallback_to_a_build_against_an_unmerged_upstream(
    base_sha: list[Sha], monkeypatch: pytest.MonkeyPatch
) -> None:
    """middle as an upstream's downstream CI built it, against base's commit under test, is never reused."""
    store = _Store()
    deps = [_spec("middle", allow_stale=True)]
    with monkeypatch.context() as m:
        m.setattr(resolve_deps, "probe_workflow_runs", lambda repo, sha, token: WorkflowRuns(state="running"))
        under_test = _resolve(store, deps, allow_stale=False, pins={Repo("o/base"): Ref(_UNMERGED)})
    store.tars += [under_test["base"].artifact_name, under_test["middle"].artifact_name]
    base_sha[0] = _NEW

    assert _resolve(store, deps, allow_stale=True)["middle"].source == "triggered rebuild"


def test_every_resolved_artifact_records_its_dep_tree(base_sha: list[Sha]) -> None:
    store = _Store()
    got = _resolve(store, [_spec("middle")], allow_stale=False)
    middle = store.sidecars[got["middle"].artifact_name]
    assert middle["direct"] == [got["base"].artifact_name]
    assert [d["artifact-name"] for d in middle["deps"]] == [got["base"].artifact_name]
    assert store.sidecars[got["base"].artifact_name]["deps"] == []
    assert any(name.startswith(f"top-{'d' * 40}-") for name in store.sidecars)


_MANIFEST: Final = """
[package]
visibility = "private"
name = "top"
repo = "o/top"
compiler-inputs = []
{package_extra}

[[deps]]
repo = "o/a"
package = "a"
ref = "main"

[[deps]]
repo = "o/b"
package = "b"
ref = "main"
allow-stale = false

[[deps]]
package = "top-extra"

[packages.top-extra]
compiler-inputs = []
"""


@pytest.mark.parametrize(
    ("package_extra", "expected"), [("", (False, False)), ("allow-stale-deps = true", (True, False))]
)
def test_allow_stale_defaults_to_the_package_setting(package_extra: str, expected: tuple[bool, bool]) -> None:
    m = parse_manifest(_MANIFEST.format(package_extra=package_extra))
    assert tuple(d.allow_stale for d in m.deps if not d.sibling) == expected
    assert not next(d for d in m.deps if d.sibling).allow_stale


def test_allow_stale_on_a_package_of_this_repo_is_rejected() -> None:
    with pytest.raises(ValidationError, match="'allow-stale' without 'repo'"):
        DepTable.model_validate({"package": "x", "allow-stale": True})


_TOP_MANIFEST: Final = """
[package]
visibility = "private"
name = "top"
repo = "o/top"
compiler-inputs = []

[[matrix.build.include]]
platform = "ubuntu-24.04"
"""


@pytest.mark.parametrize(
    ("event", "branch", "pin", "falls_back"),
    [
        ("pull_request", "feature", "", True),
        ("pull_request_target", "main", f"o/top@{'a' * 40}", True),
        ("push", "main", "", False),
        ("pull_request", "main", f"o/base@{'a' * 40}", False),
        ("pull_request", "sync/x", "", False),
    ],
)
def test_only_pull_requests_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, event: str, branch: str, pin: str, falls_back: bool
) -> None:
    manifest = tmp_path / "manifest.toml"
    manifest.write_text(_TOP_MANIFEST)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
    monkeypatch.setattr(resolve_deps, "select_token", lambda: None)
    monkeypatch.setattr(resolve_deps, "resolve_ref_to_sha", lambda repo, ref, token: Sha("d" * 40))
    monkeypatch.setattr(resolve_deps, "bfs_load_manifests", lambda **kw: ({}, {}))
    monkeypatch.setattr(s3_store.ArtifactIndex, "flush_sidecars", lambda self: 0)
    seen: list[StaleFallback | None] = []
    real = resolve_deps.resolve_packages

    def resolve_packages(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["stale"])
        return real(*args, **kwargs)

    monkeypatch.setattr(resolve_deps, "resolve_packages", resolve_packages)
    resolve_deps._run(str(manifest), branch, "build", "", ".ci/manifest.toml", pin=pin)
    assert [s is not None for s in seen] == [falls_back]


_TWO_PACKAGES: Final = """
[package]
visibility = "private"
name = "top"
repo = "o/top"
compiler-inputs = ["cxx-compiler"]
meta = true

[packages.p1]
compiler-inputs = ["cxx-compiler"]
deps = [{ repo = "o/middle", package = "middle", ref = "main", allow-stale = true }]

[packages.p2]
compiler-inputs = ["cxx-compiler"]
deps = [{ repo = "o/middle", package = "middle", ref = "main" }]
"""


def test_packages_of_one_job_never_get_two_builds_of_a_dep(base_sha: list[Sha]) -> None:
    store = _Store()
    _published_then_base_moves(base_sha, store, [_spec("middle", allow_stale=True)])
    manifest = parse_manifest(_TWO_PACKAGES)

    def resolve(prefixes: list[str]) -> dict[str, ResolvedDep]:
        deps, _ = resolve_deps.resolve_packages(
            manifest,
            [PackageName(p) for p in prefixes],
            own_sha=Sha("d" * 40),
            own_ref=Ref("main"),
            matrix_entry=dict(_LEG),
            manifest_cache={(Repo("o/middle"), Ref("main")): parse_manifest(_MIDDLE_MANIFEST)},
            sync_branch=None,
            sync_exists_by_repo={},
            sha_cache={},
            artifact_index=store,
            run_state_cache={},
            token=None,
            can_dispatch=True,
            lane=EXECUTION_RUNNER,
            dispatch_plans={},
            stale=StaleFallback(token=None),
        )
        return {str(d.name): d for d in deps}

    assert resolve(["p1"])["middle"].source == "stale artifact"
    assert resolve(["p1", "p2"])["middle"].source == "triggered rebuild"
