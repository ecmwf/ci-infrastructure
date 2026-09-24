#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the transitive dependency tree of every matrix leg in .ci/manifest.toml (schema: `manifest`).

Upstream manifests are fetched with one batched GraphQL query per BFS layer.

Outputs (to $GITHUB_OUTPUT, or stdout)::

    matrix-<name>=<JSON: {"include": [...]}>
        Each leg plus a '_resolved' object:
          _resolved.cmake-prefix-path     semicolon-separated install paths
          _resolved.all-artifact-names    space-separated transitive artifact names
          _resolved.own-*                 own-name, own-artifact-name, own-tar-name, own-sha,
                                          own-ref, own-platform, own-compiler,
                                          own-build-type, own-python, own-deps-hash
          _resolved.deps                  list of {name, repo, ref, sha, artifact-name,
                                                   source, needs-python, install-path}
          _resolved.ctest                 this kind's [matrix.<kind>].ctest (false if unset)
          _resolved.ctest-args            this kind's [matrix.<kind>].ctest-args ("" if unset)
          _resolved.job-name              job title without its lane prefix, used as
                                          `name: build+test (${{ matrix._resolved['job-name'] }})`

    json=<JSON: all blocks keyed by matrix name>
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, NewType

import click

from . import job_names, s3_store
from ._errors import CIError
from ._github_api import (
    _OPTION_TOKEN_RE,
    EXECUTION_RUNNER,
    Execution,
    ManifestSchemaError,
    _gh,
    compute_deps_hash8,
    compute_platform_slug,
    fetch_manifests_layer,
    lane_suffix,
    probe_workflow_runs,
    resolve_reuse_matrix,
    select_token,
    template_version_for_lane,
    write_outputs,
)
from ._github_api import make_artifact_name as _make_artifact_name
from ._github_api import resolve_ref_to_sha as _resolve_ref_to_sha
from .manifest import DepTable
from .manifest import validate as validate_manifest
from .runners import resolve_runner
from .sync_branch import is_sync_branch

# Leg fields that enter the artifact name; `runs-on`/`container` are scheduling only.
_MATRIX_DISCRIMINATORS: Final = frozenset(
    {
        "platform",
        "build-type",
        "compiler",
        "cxx-compiler",
        "fortran-compiler",
        "python-version",
    }
)


def _as_option(raw: Any, context: str) -> str:
    """'' if absent; one name maps to one CMake preset."""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, (list, tuple)):
        raise ResolveError(
            f"{context}: 'options' must be a scalar config name, not a list "
            f"({raw!r}); name the combination explicitly (e.g. 'a-b')"
        )
    if not isinstance(raw, str):
        raise ResolveError(f"{context}: 'options' must be a string, got {type(raw).__name__}")
    if not _OPTION_TOKEN_RE.fullmatch(raw):
        raise ResolveError(f"{context}: invalid build option {raw!r}: only [A-Za-z0-9_-] allowed")
    return raw


Repo = NewType("Repo", str)
PackageName = NewType("PackageName", str)  # the [package].prefix
Ref = NewType("Ref", str)
Sha = NewType("Sha", str)
ArtifactName = NewType("ArtifactName", str)


def as_repo(s: str) -> Repo:
    if "/" not in s or s.count("/") != 1 or not all(s.split("/", 1)):
        raise ValueError(f"repo must be 'owner/name', got {s!r}")
    return Repo(s)


class ResolveError(Exception):
    """Resolver gave up."""


@dataclass(frozen=True)
class DepSpec:
    repo: Repo
    package: PackageName
    ref: Ref
    compiler_inputs: Sequence[str]
    build_type_input: str
    platform_input: str
    needs_python: bool
    python_version_input: str
    # Options do not propagate: `option` is a fixed literal, `options_input` names a
    # leg field to read it from; both empty consumes the plain build.
    option: str = ""
    options_input: str | None = None
    # `options` does not propagate, so a `when` on it in a repo with consumers can make both sides disagree.
    when: Mapping[str, frozenset[str]] | None = None

    def applies_to(self, leg: Mapping[str, Any]) -> bool:
        """Compared as str; a missing field never matches."""
        if self.when is None:
            return True
        return all(str(leg.get(field, "")) in accepted for field, accepted in self.when.items())


@dataclass(frozen=True)
class PackageSpec:
    name: str
    prefix: PackageName
    repo: Repo
    compiler_inputs: Sequence[str]


@dataclass(frozen=True)
class CtestSpec:
    enabled: bool = False
    args: str = ""


@dataclass
class Manifest:
    package: PackageSpec
    deps: list[DepSpec] = field(default_factory=list)
    matrix: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Kinds publishing a secondary artifact under their own prefix; others use package.prefix.
    artifact_prefix_by_kind: dict[str, str] = field(default_factory=dict)
    ctest_by_kind: dict[str, CtestSpec] = field(default_factory=dict)
    # Picks which of a producer's lane workflows a recovery rebuild fires.
    execution_by_kind: dict[str, Execution] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedDep:
    name: PackageName
    repo: Repo
    ref: Ref
    sha: Sha
    artifact_name: ArtifactName
    cached: bool  # already in the S3 store at resolve time
    # "artifact": in the store, or a producer run in flight will upload it.
    # "triggered rebuild": timing skew; _run dispatches the producer before exiting.
    source: Literal["artifact", "triggered rebuild"]
    needs_python: bool
    install_path: Path
    # The fields artifact_name is built from; the name alone is ambiguous to parse.
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    deps_hash: str | None

    def to_json(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "repo": self.repo,
            "ref": self.ref,
            "sha": self.sha,
            "artifact-name": self.artifact_name,
            "cached": self.cached,
            "source": self.source,
            "needs-python": self.needs_python,
            "install-path": str(self.install_path),
            "platform": self.platform,
            "compiler": self.compiler or "",
            "build-type": self.build_type,
            "python-version": self.python_version or "",
            "deps-hash": self.deps_hash or "",
        }


@dataclass(frozen=True)
class ResolvedOwn:
    artifact_name: ArtifactName
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    deps_hash: str | None


@dataclass(frozen=True)
class DispatchPlan:
    """A producer whose cross-repo-trigger{,-hpc}.yml must fire before fetch_deps runs.

    `lane` is the consumer kind's lane; orchestrators only wire a lane to the same lane.
    """

    repo: Repo
    ref: Ref
    sha: Sha
    lane: Execution


def _to_dep_spec(t: DepTable) -> DepSpec:
    return DepSpec(
        repo=Repo(t.repo),
        package=PackageName(t.package),
        ref=Ref(t.ref),
        compiler_inputs=list(t.compiler_inputs),
        build_type_input=t.build_type_input,
        platform_input=t.platform_input,
        needs_python=t.needs_python,
        python_version_input=t.python_version_input,
        option=t.options,
        options_input=t.options_input,
        when=None if t.when is None else {k: frozenset(v) for k, v in t.when.items()},
    )


def parse_manifest(text: str, default_repo: str | None = None) -> Manifest:
    try:
        raw = validate_manifest(tomllib.loads(text))
    except ManifestSchemaError as e:
        raise ValueError(str(e)) from e
    repo = raw.package.repo or default_repo
    if not repo:
        raise ValueError("manifest [package] must define 'repo' (or pass --self-repo)")

    blocks = {
        k: {"reuse-matrix": b.reuse_matrix, "include": b.include, "defaults": b.defaults} for k, b in raw.matrix.items()
    }
    matrix: dict[str, list[dict[str, Any]]] = {}
    for kind, body in raw.matrix.items():
        try:
            matrix[kind] = list(resolve_reuse_matrix(kind, body.include, body.reuse_matrix, blocks))
        except ManifestSchemaError as e:
            raise ValueError(str(e)) from e

    return Manifest(
        package=PackageSpec(
            name=raw.package.name,
            prefix=PackageName(raw.package.prefix),
            repo=as_repo(repo),
            compiler_inputs=list(raw.package.compiler_inputs),
        ),
        deps=[_to_dep_spec(d) for d in raw.deps],
        matrix=matrix,
        artifact_prefix_by_kind={k: b.artifact_prefix for k, b in raw.matrix.items() if b.artifact_prefix is not None},
        ctest_by_kind={k: CtestSpec(enabled=b.ctest, args=b.ctest_args.strip()) for k, b in raw.matrix.items()},
        execution_by_kind={k: b.execution for k, b in raw.matrix.items()},
    )


def resolve_ref_to_sha(repo: Repo, ref: Ref, token: str | None) -> Sha:
    return Sha(_resolve_ref_to_sha(repo, ref, token))


def _resolve_own_sha(own_repo: str, current_branch: str, token: str | None) -> Sha:
    """Never GITHUB_SHA: a PR merge commit no consumer can resolve."""
    if not current_branch:
        raise ResolveError("Cannot determine own artifact SHA: --current-branch is required")
    return resolve_ref_to_sha(Repo(own_repo), Ref(current_branch), token)


def producer_can_build(producer_manifest: Manifest, matrix_entry: Mapping[str, Any]) -> bool:
    """True if some producer leg matches on the discriminators both declare, and on options."""
    if not producer_manifest.matrix:
        return True
    # Unlike the other discriminators, an omitted `options` is the concrete plain config.
    consumer_keys = _MATRIX_DISCRIMINATORS & matrix_entry.keys()
    req_option = _as_option(matrix_entry.get("options"), context="requested option")

    def _leg_matches(leg: Mapping[str, Any]) -> bool:
        if any(str(leg[k]) != str(matrix_entry[k]) for k in consumer_keys & leg.keys()):
            return False
        return _as_option(leg.get("options"), context="producer leg option") == req_option

    return any(_leg_matches(leg) for legs in producer_manifest.matrix.values() for leg in legs)


def is_normal_ref(
    spec: DepSpec,
    ref: Ref,
    sync_branch: Ref | None,
    sync_exists_by_repo: Mapping[Repo, bool],
) -> bool:
    """Declared ref or a valid sync-branch override; only then is auto-dispatch allowed."""
    if sync_branch and sync_exists_by_repo.get(spec.repo, False):
        return ref == sync_branch
    return ref == spec.ref


def dispatch_producer_workflow(
    *,
    plan: DispatchPlan,
    dispatcher_repo: str,
    dispatcher_sha: str,
    branch: str,
    fallback_ref: str,
    token: str,
) -> None:
    """Dispatch the producer's lane workflow with rebuild-request, then wait ≤60s for its run to appear.

    Without the wait, fetch_deps could see no run yet and bail instead of polling.
    """
    dispatch_id = (
        f"resolve-{os.environ.get('GITHUB_RUN_ID', '0')}-"
        f"{os.environ.get('GITHUB_RUN_ATTEMPT', '0')}-{secrets.token_hex(4)}"
    )
    workflow_file = f"cross-repo-trigger{lane_suffix(plan.lane)}.yml"
    cmd = [
        "gh",
        "workflow",
        "run",
        workflow_file,
        "--repo",
        plan.repo,
        "--ref",
        plan.ref,
        "-f",
        f"dispatch-id={dispatch_id}",
        "-f",
        f"from-repo={dispatcher_repo}",
        "-f",
        f"from-sha={dispatcher_sha}",
        "-f",
        "rebuild-request=true",
        "-f",
        f"branch={branch}",
        "-f",
        f"fallback-ref={fallback_ref}",
    ]
    rc, _, stderr = _gh(cmd, token)
    if rc != 0:
        raise ResolveError(
            f"Failed to dispatch {workflow_file} in {plan.repo}@{plan.ref} "
            f"(dispatcher {dispatcher_repo}@{dispatcher_sha[:8]}): {stderr.strip()}"
        )

    print(
        f"::notice::Triggering REBUILD of upstream {plan.repo}@{plan.ref} (sha={plan.sha[:8]}, "
        f"lane={plan.lane}): its artifact was missing, so it will recompile. Downstream build jobs "
        "will WAIT for it to finish."
    )
    print(
        f"  dispatched {workflow_file} in {plan.repo}@{plan.ref} (sha={plan.sha[:8]}, "
        f"dispatch-id={dispatch_id}); "
        "waiting for run to appear"
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if probe_workflow_runs(plan.repo, plan.sha, token).in_flight:
            print(f"  {plan.repo}@{plan.sha[:8]} run is now visible; resolve job exiting")
            return
        time.sleep(2)
    print(
        f"::warning::Dispatched run for {plan.repo}@{plan.sha[:8]} did not appear within 60s. "
        "fetch_deps will fail to find it if visibility doesn't catch up."
    )


def make_artifact_name(
    prefix: PackageName,
    sha: Sha,
    deps_hash8: str | None,
    platform_slug: str,
    compiler: str | None,
    build_type: str,
    python_version: str | None,
    option: str = "",
    *,
    template_version: int = 0,
) -> ArtifactName:
    return ArtifactName(
        _make_artifact_name(
            prefix,
            sha,
            deps_hash8,
            platform_slug,
            compiler,
            build_type,
            python_version,
            option,
            template_version=template_version,
        )
    )


def _install_base() -> Path:
    """Literal `$RUNNER_TEMP/install`, expanded by the consuming job: it may run on another runner."""
    return Path("$RUNNER_TEMP") / "install"


def _join_compilers(
    compiler_inputs: Sequence[str],
    matrix_entry: Mapping[str, Any],
    context: str,
) -> str | None:
    """In alphabetical field-name order."""
    if not compiler_inputs:
        return None
    parts: list[str] = []
    for field_name in sorted(compiler_inputs):
        if field_name not in matrix_entry:
            raise ResolveError(
                f"{context}: compiler-inputs references matrix field "
                f"'{field_name}', which is not set on this matrix entry. "
                f"Available fields: {sorted(matrix_entry)}"
            )
        value = matrix_entry[field_name]
        if not value:
            raise ResolveError(
                f"{context}: matrix field '{field_name}' is empty: {matrix_entry!r}. "
                "Set a compiler binary like 'clang++-18' or 'gfortran-14'."
            )
        parts.append(str(value))
    return "-".join(parts)


def _classify_orphan_pin(
    *,
    spec: DepSpec,
    ref: Ref,
    sha: Sha,
    artifact_name: ArtifactName,
    matrix_entry: Mapping[str, Any],
    manifest_cache: Mapping[tuple[Repo, Ref], Manifest],
    sync_branch: Ref | None,
    sync_exists_by_repo: Mapping[Repo, bool],
    can_dispatch: bool,
    lane: Execution,
    dispatch_plans: dict[tuple[Repo, Ref, Execution], DispatchPlan],
) -> Literal["triggered rebuild"]:
    """Artifact missing, no producer CI in flight: raise, or (timing skew) record a DispatchPlan."""
    producer_manifest = manifest_cache.get((spec.repo, ref))
    if producer_manifest is not None and not producer_can_build(producer_manifest, matrix_entry):
        # Only the fields the producer discriminates on are negotiable.
        producer_keys = set().union(*(leg.keys() for legs in producer_manifest.matrix.values() for leg in legs))
        relevant_keys = _MATRIX_DISCRIMINATORS & matrix_entry.keys() & producer_keys
        relevant = {k: matrix_entry[k] for k in sorted(relevant_keys)}
        req_option = _as_option(matrix_entry.get("options"), context="requested option")
        if req_option:
            relevant["options"] = req_option
        raise ResolveError(
            f"dep '{spec.package}' from {spec.repo}@{ref} cannot satisfy this matrix leg: "
            f"the producer's manifest declares no [matrix.<kind>.include] row matching {relevant}. "
            "Either add the missing matrix leg to the producer or drop the unsupported "
            "combination from this consumer's manifest."
        )

    if not is_normal_ref(spec, ref, sync_branch, sync_exists_by_repo):
        raise ResolveError(
            f"dep '{spec.package}' from {spec.repo}@{ref} is pinned to a non-default ref but "
            f"no artifact named '{artifact_name}' exists in the producer's store and no CI is "
            "in flight for that commit. Refusing to auto-rebuild on a divergent ref — fix the pin "
            f"in the consumer manifest (declared ref was {spec.ref!r}), or trigger the producer "
            "CI manually for that ref."
        )

    if not can_dispatch:
        raise ResolveError(
            f"dep '{spec.package}' from {spec.repo}@{ref} resolved to {sha}, "
            f"but no artifact named '{artifact_name}' is in the producer's store "
            "and no producer CI is in flight for that commit. The pin is orphaned. "
            "Pass client-id and app-private-key to actions/resolve-deps to enable "
            "auto-recovery via consumer-driven dispatch."
        )

    if producer_manifest is not None and lane not in producer_manifest.execution_by_kind.values():
        raise ResolveError(
            f"dep '{spec.package}' from {spec.repo}@{ref} is missing its {lane} artifact "
            f"'{artifact_name}', but the producer's manifest declares no [matrix.<kind>] with "
            f"execution = '{lane}', so it has no cross-repo-trigger{lane_suffix(lane)}.yml to rebuild "
            "it. Add the lane to the producer, or drop the dep from this consumer's "
            f"{lane} kinds."
        )

    # Keyed by lane too: a producer needed by both lanes needs two dispatches.
    dispatch_plans.setdefault((spec.repo, ref, lane), DispatchPlan(repo=spec.repo, ref=ref, sha=sha, lane=lane))
    return "triggered rebuild"


def resolve_leg(
    own: PackageSpec,
    own_deps: Sequence[DepSpec],
    own_sha: Sha,
    matrix_entry: Mapping[str, Any],
    manifest_cache: Mapping[tuple[Repo, Ref], Manifest],
    sync_branch: Ref | None,
    sync_exists_by_repo: Mapping[Repo, bool],
    sha_cache: dict[tuple[Repo, Ref], Sha],
    artifact_cache: dict[ArtifactName, bool],
    run_state_cache: dict[tuple[Repo, Sha], bool],
    token: str | None,
    can_dispatch: bool,
    lane: Execution,
    dispatch_plans: dict[tuple[Repo, Ref, Execution], DispatchPlan],
    own_prefix_override: str | None = None,
) -> tuple[list[ResolvedDep], ResolvedOwn]:
    """Transitive deps (leaves first) and the OWN artifact of one matrix entry."""
    visited: dict[PackageName, ResolvedDep] = {}
    order: list[PackageName] = []

    def visit(spec: DepSpec, parent_ctx: Mapping[str, Any]) -> ResolvedDep:
        if spec.package in visited:
            return visited[spec.package]

        compiler = _join_compilers(spec.compiler_inputs, parent_ctx, context=f"dep '{spec.package}'")

        build_type = str(parent_ctx.get(spec.build_type_input, "Release"))
        platform = str(parent_ctx.get(spec.platform_input, ""))
        platform_slug = compute_platform_slug(platform)
        python_version: str | None = str(parent_ctx.get(spec.python_version_input, "")) if spec.needs_python else None

        if spec.option:
            dep_option = spec.option
        elif spec.options_input is not None:
            dep_option = _as_option(parent_ctx.get(spec.options_input), context=f"dep '{spec.package}'")
        else:
            dep_option = ""

        ref = sync_branch if sync_branch and sync_exists_by_repo.get(spec.repo, False) else spec.ref

        # Sub-deps first, against the same leg, filtered by `when` exactly as the
        # upstream's own CI did, so deps-hash8 reproduces the published name.
        sub_deps: list[ResolvedDep] = []
        sub_manifest = manifest_cache.get((spec.repo, ref))
        if sub_manifest is not None:
            for sub_spec in sub_manifest.deps:
                if not sub_spec.applies_to(parent_ctx):
                    continue
                sub_deps.append(visit(sub_spec, parent_ctx))

        sha_key = (spec.repo, ref)
        if sha_key not in sha_cache:
            sha_cache[sha_key] = resolve_ref_to_sha(spec.repo, ref, token)
        sha = sha_cache[sha_key]

        deps_hash8 = compute_deps_hash8([d.artifact_name for d in sub_deps])

        artifact_name = make_artifact_name(
            prefix=spec.package,
            sha=sha,
            deps_hash8=deps_hash8,
            platform_slug=platform_slug,
            compiler=compiler,
            build_type=build_type,
            python_version=python_version,
            option=dep_option,
            template_version=template_version_for_lane(lane),
        )

        if artifact_name not in artifact_cache:
            artifact_cache[artifact_name] = s3_store.object_exists(artifact_name)
        cached = artifact_cache[artifact_name]

        if cached:
            source: Literal["artifact", "triggered rebuild"] = "artifact"
        else:
            run_key = (spec.repo, sha)
            if run_key not in run_state_cache:
                run_state_cache[run_key] = probe_workflow_runs(spec.repo, sha, token).in_flight
            if run_state_cache[run_key]:
                source = "artifact"  # fetch_deps polls for the in-flight upload
            else:
                # Match what THIS dep requests: build-type / platform / options can differ per dep.
                requested_ctx = {
                    **parent_ctx,
                    "build-type": build_type,
                    "platform": platform,
                    "options": dep_option,
                }
                source = _classify_orphan_pin(
                    spec=spec,
                    ref=ref,
                    sha=sha,
                    artifact_name=artifact_name,
                    matrix_entry=requested_ctx,
                    manifest_cache=manifest_cache,
                    sync_branch=sync_branch,
                    sync_exists_by_repo=sync_exists_by_repo,
                    can_dispatch=can_dispatch,
                    lane=lane,
                    dispatch_plans=dispatch_plans,
                )

        resolved = ResolvedDep(
            name=spec.package,
            repo=spec.repo,
            ref=ref,
            sha=sha,
            artifact_name=artifact_name,
            cached=cached,
            source=source,
            needs_python=spec.needs_python,
            install_path=_install_base() / spec.package,
            platform=platform_slug,
            compiler=compiler,
            build_type=build_type,
            python_version=python_version,
            deps_hash=deps_hash8,
        )
        visited[spec.package] = resolved
        order.append(spec.package)
        return resolved

    applicable_deps = [spec for spec in own_deps if spec.applies_to(matrix_entry)]
    for spec in applicable_deps:
        visit(spec, matrix_entry)

    deps_resolved = [visited[name] for name in order]

    direct_dep_artifact_names = [visited[s.package].artifact_name for s in applicable_deps]
    own_compiler = _join_compilers(own.compiler_inputs, matrix_entry, context=f"[package] '{own.name}'")
    own_build_type = str(matrix_entry.get("build-type", "Release"))
    own_platform = compute_platform_slug(str(matrix_entry.get("platform", "")))
    # The leg's python-version decides, not needs-python (which only drives pip installs of deps).
    own_python_raw = str(matrix_entry.get("python-version", "")).strip()
    own_python: str | None = own_python_raw or None
    own_option = _as_option(matrix_entry.get("options"), context=f"[package] '{own.name}'")

    own_deps_hash = compute_deps_hash8(direct_dep_artifact_names)
    own_artifact = make_artifact_name(
        prefix=PackageName(own_prefix_override) if own_prefix_override else own.prefix,
        sha=own_sha,
        deps_hash8=own_deps_hash,
        platform_slug=own_platform,
        compiler=own_compiler,
        build_type=own_build_type,
        python_version=own_python,
        option=own_option,
        template_version=template_version_for_lane(lane),
    )

    return deps_resolved, ResolvedOwn(
        artifact_name=own_artifact,
        platform=own_platform,
        compiler=own_compiler,
        build_type=own_build_type,
        python_version=own_python,
        deps_hash=own_deps_hash,
    )


def bfs_load_manifests(
    root_deps: Sequence[DepSpec],
    sync_branch: Ref | None,
    token: str | None,
    manifest_path: str,
    max_depth: int = 8,
) -> tuple[dict[tuple[Repo, Ref], Manifest], dict[Repo, bool]]:
    """Returns (manifests, sync_branch exists per repo)."""
    manifest_cache: dict[tuple[Repo, Ref], Manifest] = {}
    sync_exists: dict[Repo, bool] = {}
    queue: list[tuple[Repo, Ref]] = [(d.repo, d.ref) for d in root_deps]

    for _depth in range(max_depth):
        layer = [(r, ref) for (r, ref) in queue if (r, ref) not in manifest_cache]
        if not layer:
            break

        results = fetch_manifests_layer(layer, sync_branch, token, manifest_path)

        next_queue: list[tuple[Repo, Ref]] = []
        for (raw_repo, raw_ref), (text, sync_present) in results.items():
            repo, ref = Repo(raw_repo), Ref(raw_ref)
            if sync_branch:
                sync_exists.setdefault(repo, sync_present)
                if sync_present and ref != sync_branch:
                    next_queue.append((repo, sync_branch))
                    continue
            if text is None:
                manifest_cache[(repo, ref)] = Manifest(
                    package=PackageSpec(
                        name=repo.split("/", 1)[1],
                        prefix=PackageName(""),
                        repo=repo,
                        compiler_inputs=(),
                    ),
                    deps=[],
                )
                continue
            try:
                m = parse_manifest(text, default_repo=repo)
            except ValueError as e:
                raise ResolveError(f"Failed to parse manifest from {repo}@{ref}: {e}") from e
            manifest_cache[(repo, ref)] = m
            for sub in m.deps:
                next_queue.append((sub.repo, sub.ref))

        queue = next_queue

    return manifest_cache, sync_exists


@click.command(help="Resolve dep tree for all matrix legs in a manifest.")
@click.option("--manifest", default=".ci/manifest.toml", help="Path to local manifest TOML")
@click.option("--current-branch", "current_branch", default="", help="Branch being built (for sync-branch convention)")
@click.option(
    "--matrix",
    default="build",
    help="Which [matrix.<name>] to expand (default 'build'). Can be repeated comma-separated.",
)
@click.option(
    "--self-repo",
    "self_repo",
    default="",
    help="Override [package].repo (for testing without GITHUB_REPOSITORY env).",
)
@click.option(
    "--upstream-manifest-path",
    "upstream_manifest_path",
    default=".ci/manifest.toml",
    help="Where to look for manifests in upstream repos (default: same as local).",
)
def main(
    manifest: str,
    current_branch: str,
    matrix: str,
    self_repo: str,
    upstream_manifest_path: str,
) -> None:
    try:
        _run(manifest, current_branch, matrix, self_repo, upstream_manifest_path)
    except (ResolveError, ValueError) as e:
        raise CIError(str(e)) from e


def _run(
    manifest: str,
    current_branch: str,
    matrix: str,
    self_repo: str,
    upstream_manifest_path: str,
) -> None:
    if not os.path.exists(manifest):
        raise ResolveError(f"Manifest not found: {manifest}")

    with open(manifest) as f:
        local_manifest = parse_manifest(
            f.read(),
            default_repo=self_repo or os.environ.get("GITHUB_REPOSITORY"),
        )

    sync_branch: Ref | None = None
    if current_branch and is_sync_branch(current_branch):
        sync_branch = Ref(current_branch)

    token = select_token()

    own_sha = _resolve_own_sha(str(local_manifest.package.repo), current_branch, token)

    manifest_cache, sync_exists = bfs_load_manifests(
        root_deps=local_manifest.deps,
        sync_branch=sync_branch,
        token=token,
        manifest_path=upstream_manifest_path,
    )

    sha_cache: dict[tuple[Repo, Ref], Sha] = {}
    artifact_cache: dict[ArtifactName, bool] = {}
    run_state_cache: dict[tuple[Repo, Sha], bool] = {}
    matrices_out: dict[str, dict[str, Any]] = {}

    # Without an App token from resolve-deps, orphan pins hard-fail.
    dispatch_token = os.environ.get("DISPATCH_TOKEN", "").strip()
    can_dispatch = bool(dispatch_token)
    dispatch_plans: dict[tuple[Repo, Ref, Execution], DispatchPlan] = {}

    matrix_names = [m.strip() for m in matrix.split(",") if m.strip()]

    for mname in matrix_names:
        include = local_manifest.matrix.get(mname, [])
        if not include:
            print(f"::warning::No [matrix.{mname}.include] in manifest; skipping.", file=sys.stderr)
            continue

        out_include: list[dict[str, Any]] = []
        own_prefix_override = local_manifest.artifact_prefix_by_kind.get(mname)
        ctest = local_manifest.ctest_by_kind.get(mname, CtestSpec())
        lane = local_manifest.execution_by_kind.get(mname, EXECUTION_RUNNER)
        for entry in include:
            deps_resolved, own = resolve_leg(
                own=local_manifest.package,
                own_deps=local_manifest.deps,
                own_sha=own_sha,
                matrix_entry=entry,
                manifest_cache=manifest_cache,
                sync_branch=sync_branch,
                sync_exists_by_repo=sync_exists,
                sha_cache=sha_cache,
                artifact_cache=artifact_cache,
                run_state_cache=run_state_cache,
                token=token,
                can_dispatch=can_dispatch,
                lane=lane,
                dispatch_plans=dispatch_plans,
                own_prefix_override=own_prefix_override,
            )
            cmake_paths = [str(d.install_path) for d in deps_resolved]
            all_artifact_names = [d.artifact_name for d in deps_resolved]
            resolved_block = {
                "cmake-prefix-path": ";".join(cmake_paths),
                "all-artifact-names": " ".join(all_artifact_names),
                "all-artifact-sources": " ".join(d.source for d in deps_resolved),
                "own-name": local_manifest.package.name,
                "own-artifact-name": own.artifact_name,
                "own-sha": own_sha,
                "own-ref": current_branch,
                "own-platform": own.platform,
                "own-compiler": own.compiler or "",
                "own-build-type": own.build_type,
                "own-python": own.python_version or "",
                "own-deps-hash": own.deps_hash or "",
                "own-tar-name": f"{own.artifact_name}.tar.gz",
                "deps": [d.to_json() for d in deps_resolved],
                # Scoped-out deps were not resolved; next() would raise on them.
                "direct-artifact-names": " ".join(
                    next(d.artifact_name for d in deps_resolved if d.name == s.package)
                    for s in local_manifest.deps
                    if s.applies_to(entry)
                ),
                "ctest": ctest.enabled,
                "ctest-args": ctest.args,
                "job-name": job_names.name_suffix(entry, include, local_manifest.package.compiler_inputs),
            }
            merged = {**entry, "_resolved": resolved_block}
            # Here, not in the workflow: `runs-on: ${{ matrix['runs-on'] }}` is not re-evaluated.
            if "runs-on" in merged:
                merged["runs-on"] = resolve_runner(merged["runs-on"])
            out_include.append(merged)

        matrices_out[mname] = {"include": out_include}

    # After all legs, so a producer flagged by several legs is dispatched once.
    if dispatch_plans:
        dispatcher_repo = os.environ.get("GITHUB_REPOSITORY", "")
        branch = current_branch or os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or "main"
        rebuild_repos = ", ".join(sorted(p.repo for p in dispatch_plans.values()))
        print(
            f"::notice::{len(dispatch_plans)} upstream package(s) need a rebuild before this repo can "
            f"build: {rebuild_repos}. Dispatching their CI now; this run's build jobs will wait for them."
        )
        for plan in dispatch_plans.values():
            # fallback-ref: the producer's pinned ref, not a hardcoded "main".
            dispatch_producer_workflow(
                plan=plan,
                dispatcher_repo=dispatcher_repo,
                dispatcher_sha=str(own_sha),
                branch=branch,
                fallback_ref=str(plan.ref),
                token=dispatch_token,
            )

    outputs: dict[str, str] = {
        f"matrix-{mname}": json.dumps(mdata, separators=(",", ":")) for mname, mdata in matrices_out.items()
    }
    outputs["json"] = json.dumps(matrices_out, separators=(",", ":"))
    write_outputs(outputs)

    print(
        f"Resolved {sum(len(v['include']) for v in matrices_out.values())} matrix legs across "
        f"{len(matrices_out)} matrix block(s)."
    )
    for mname, mdata in matrices_out.items():
        for entry in mdata["include"]:
            label_bits = [f"{k}={v}" for k, v in entry.items() if k != "_resolved"]
            print(f"  [{mname}] " + " ".join(label_bits))
            print(f"    own:  {entry['_resolved']['own-artifact-name']}")
            for dep in entry["_resolved"]["deps"]:
                src = dep["source"]
                cached = "cached" if dep["cached"] else "—"
                print(f"    dep:  {dep['name']:24s} source={src:17s} {cached:>6s}  {dep['artifact-name']}")
    if sync_branch:
        sync_repos = [r for r, present in sync_exists.items() if present]
        print(f"sync-branch '{sync_branch}' active in: {', '.join(sync_repos) if sync_repos else '(none)'}")


if __name__ == "__main__":
    main()
