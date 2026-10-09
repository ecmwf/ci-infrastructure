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
          _resolved.job-name              job title without its lane prefix, used as
                                          `name: build+test (${{ matrix._resolved['job-name'] }})`

    json=<JSON: all blocks keyed by matrix name>
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
from .sync_branch import is_sync_branch

_SHA_PIN_RE: Final = re.compile(r"[0-9a-f]{40}")


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


def parse_pin(pin: str) -> dict[Repo, Ref]:
    """`owner/repo@<40-hex sha>` -> {repo: sha}; empty -> {}."""
    if not pin.strip():
        return {}
    repo, sep, sha = pin.strip().partition("@")
    if not sep or not _SHA_PIN_RE.fullmatch(sha):
        raise ResolveError(f"--pin must be owner/repo@<40-hex sha>, got {pin!r}")
    return {as_repo(repo): Ref(sha)}


class ResolveError(Exception):
    """Resolver gave up."""


@dataclass(frozen=True)
class DepSpec:
    repo: Repo
    package: PackageName
    ref: Ref
    # None: the producer package's own compiler-inputs.
    compiler_inputs: Sequence[str] | None
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
    unless: Mapping[str, frozenset[str]] | None = None
    # A package of the declaring repo, built from the declaring package's commit; `ref` is unused.
    sibling: bool = False

    def applies_to(self, leg: Mapping[str, Any], lane: Execution) -> bool:
        """Compared as str; a missing field never matches. `execution` is the lane."""
        ctx = {**leg, "execution": lane}

        def matches(predicate: Mapping[str, frozenset[str]]) -> bool:
            return all(str(ctx.get(field, "")) in accepted for field, accepted in predicate.items())

        return (self.when is None or matches(self.when)) and (self.unless is None or not matches(self.unless))


@dataclass(frozen=True)
class PackageSpec:
    name: str
    prefix: PackageName
    repo: Repo
    compiler_inputs: Sequence[str]


@dataclass(frozen=True)
class PackageInfo:
    """One package a repo publishes, or its meta `[package]`."""

    prefix: PackageName
    compiler_inputs: Sequence[str]
    deps: Sequence[DepSpec]
    meta: bool = False
    add_to_path: Sequence[str] = ()


@dataclass
class Manifest:
    package: PackageSpec
    deps: list[DepSpec] = field(default_factory=list)
    matrix: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Every package by prefix: `[package]`, `[packages.*]` and each `artifact-prefix`.
    packages: dict[PackageName, PackageInfo] = field(default_factory=dict)
    # The prefixes each kind publishes; empty for a kind that publishes nothing.
    packages_by_kind: dict[str, tuple[PackageName, ...]] = field(default_factory=dict)
    # The prefixes each kind builds, published or not.
    built_by_kind: dict[str, tuple[PackageName, ...]] = field(default_factory=dict)
    # Picks which of a producer's lane workflows a recovery rebuild fires.
    execution_by_kind: dict[str, Execution] = field(default_factory=dict)

    def external_deps(self) -> list[DepSpec]:
        """The deps of every package that live in another repo."""
        infos = self.packages.values() or [PackageInfo(self.package.prefix, (), self.deps)]
        return [d for info in infos for d in info.deps if not d.sibling]


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
    # "local": built earlier in the same job; never fetched.
    source: Literal["artifact", "triggered rebuild", "local"]
    needs_python: bool
    install_path: Path
    # The fields artifact_name is built from; the name alone is ambiguous to parse.
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    deps_hash: str | None
    # Install-relative dirs the producer asks its consumers to put on PATH.
    add_to_path: Sequence[str] = ()

    def to_json(self) -> dict[str, str | bool | list[str] | None]:
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
            "add-to-path": list(self.add_to_path),
        }


@dataclass(frozen=True)
class ResolvedOwn:
    artifact_name: ArtifactName
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    deps_hash: str | None
    direct_artifact_names: tuple[ArtifactName, ...] = ()


@dataclass(frozen=True)
class DispatchPlan:
    """A producer whose cross-repo-trigger{,-hpc-atos}.yml must fire before fetch_deps runs.

    `lane` is the consumer kind's lane; orchestrators only wire a lane to the same lane.
    """

    repo: Repo
    ref: Ref
    sha: Sha
    lane: Execution


def _to_dep_specs(t: DepTable, own_repo: str) -> list[DepSpec]:
    """One spec per package of a list-valued `package`."""
    return [_to_dep_spec(t, p, own_repo) for p in t.packages]


def _to_dep_spec(t: DepTable, package: str, own_repo: str) -> DepSpec:
    return DepSpec(
        repo=Repo(t.repo or own_repo),
        package=PackageName(package),
        ref=Ref(t.ref or ""),
        compiler_inputs=None if t.compiler_inputs is None else list(t.compiler_inputs),
        build_type_input=t.build_type_input,
        platform_input=t.platform_input,
        needs_python=t.needs_python,
        python_version_input=t.python_version_input,
        option=t.options,
        options_input=t.options_input,
        when=None if t.when is None else {k: frozenset(v) for k, v in t.when.items()},
        unless=None if t.unless is None else {k: frozenset(v) for k, v in t.unless.items()},
        sibling=t.repo is None,
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
        if any(not isinstance(leg.get("ctest-args", ""), str) for leg in matrix[kind]):
            raise ValueError(f"[matrix.{kind}] ctest-args must be a string")

    own_deps = [s for d in raw.deps for s in _to_dep_specs(d, repo)]
    own = PackageInfo(
        prefix=PackageName(raw.package.prefix),
        compiler_inputs=list(raw.package.compiler_inputs),
        deps=own_deps,
        meta=raw.package.meta,
        add_to_path=raw.package.add_to_path,
    )
    packages = {own.prefix: own}
    for prefix, entry in raw.packages.items():
        packages[PackageName(prefix)] = PackageInfo(
            prefix=PackageName(prefix),
            compiler_inputs=list(entry.compiler_inputs),
            deps=[s for d in entry.deps for s in _to_dep_specs(d, repo)],
            add_to_path=entry.add_to_path,
        )
    # An artifact-prefix is [package] under another name: same compilers, same deps.
    for body in raw.matrix.values():
        if body.artifact_prefix is not None and body.artifact_prefix not in packages:
            packages[PackageName(body.artifact_prefix)] = replace(own, prefix=PackageName(body.artifact_prefix))

    return Manifest(
        package=PackageSpec(
            name=raw.package.name,
            prefix=PackageName(raw.package.prefix),
            repo=as_repo(repo),
            compiler_inputs=list(raw.package.compiler_inputs),
        ),
        deps=own_deps,
        matrix=matrix,
        packages=packages,
        packages_by_kind={k: tuple(PackageName(p) for p in raw.published_by(k)) for k in raw.matrix},
        built_by_kind={k: tuple(PackageName(p) for p in raw.built_by(k)) for k in raw.matrix},
        execution_by_kind={k: b.execution for k, b in raw.matrix.items()},
    )


def resolve_ref_to_sha(repo: Repo, ref: Ref, token: str | None) -> Sha:
    return Sha(_resolve_ref_to_sha(repo, ref, token))


def _resolve_own_sha(own_repo: str, current_branch: str, token: str | None) -> Sha:
    """Never GITHUB_SHA: a PR merge commit no consumer can resolve."""
    if not current_branch:
        raise ResolveError("Cannot determine own artifact SHA: --current-branch is required")
    return resolve_ref_to_sha(Repo(own_repo), Ref(current_branch), token)


@dataclass(frozen=True)
class Variant:
    """The segments of an artifact name besides prefix, SHA and deps-hash."""

    lane: Execution
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    option: str

    def __str__(self) -> str:
        parts = [self.platform, self.compiler, f"py{self.python_version}" if self.python_version else None]
        parts += [self.build_type, f"opts.{self.option}" if self.option else None]
        return f"{'-'.join(p for p in parts if p)} ({self.lane})"


def producer_variants(producer_manifest: Manifest, package: str) -> list[Variant] | None:
    """What the producer's own legs name `package`; None if it declares no matrix to tell."""
    if not producer_manifest.matrix:
        return None
    return [v for v, _ in _publishing_legs(producer_manifest, package)]


def _publishing_legs(producer_manifest: Manifest, package: str) -> list[tuple[Variant, Mapping[str, Any]]]:
    """Each leg that publishes `package`, with the variant it names."""
    info = producer_manifest.packages.get(PackageName(package))
    compiler_inputs = info.compiler_inputs if info is not None else producer_manifest.package.compiler_inputs
    kinds = [k for k, pkgs in producer_manifest.packages_by_kind.items() if package in pkgs]
    out: list[tuple[Variant, Mapping[str, Any]]] = []
    for kind, legs in producer_manifest.matrix.items():
        if kinds and kind not in kinds:
            continue
        for leg in legs:
            try:
                variant = Variant(
                    lane=producer_manifest.execution_by_kind.get(kind, EXECUTION_RUNNER),
                    platform=compute_platform_slug(str(leg.get("platform", ""))),
                    compiler=_join_compilers(compiler_inputs, leg, context=f"[matrix.{kind}]"),
                    build_type=str(leg.get("build-type", "Release")),
                    python_version=str(leg.get("python-version", "")).strip() or None,
                    option=_as_option(leg.get("options"), context=f"[matrix.{kind}]"),
                )
            except (ResolveError, ValueError):
                continue  # a leg the producer's own resolve rejects publishes nothing
            out.append((variant, leg))
    return out


def _fields_read_by(specs: Sequence[DepSpec], compiler_inputs: Sequence[str]) -> set[str]:
    """The leg fields that decide which deps apply and what each resolves to."""
    fields = set(compiler_inputs)
    for s in specs:
        fields |= set(s.when or {}) | set(s.unless or {}) | set(s.compiler_inputs or ())
        fields |= {s.build_type_input, s.platform_input}
        fields |= {s.python_version_input} if s.needs_python else set()
        fields |= {s.options_input} if s.options_input else set()
    return fields - {"execution"}


def producer_leg(
    producer_manifest: Manifest, package: str, variant: Variant, specs: Sequence[DepSpec]
) -> Mapping[str, Any] | None:
    """The producer's own leg that publishes `variant` of `package`; None if none does.

    Legs that publish the same variant must agree on every field `specs` read, or the
    artifact name would depend on which of them ran.
    """
    legs = [leg for v, leg in _publishing_legs(producer_manifest, package) if v == variant]
    if not legs:
        return None
    info = producer_manifest.packages.get(PackageName(package))
    compiler_inputs = info.compiler_inputs if info is not None else producer_manifest.package.compiler_inputs
    fields = sorted(_fields_read_by(specs, compiler_inputs))
    differing = [f for f in fields if len({str(leg.get(f, "")) for leg in legs}) > 1]  # unset reads as ""
    if differing:
        raise ResolveError(
            f"{producer_manifest.package.repo} has {len(legs)} legs publishing {package} as {variant}, "
            f"which differ in {differing}, fields its deps read; the artifact name would depend on "
            "which leg ran. Make them differ in the name too (e.g. options), or drop one."
        )
    return legs[0]


def is_normal_ref(
    spec: DepSpec,
    ref: Ref,
    sync_branch: Ref | None,
    sync_exists_by_repo: Mapping[Repo, bool],
) -> bool:
    """Declared ref or a valid sync-branch/ or feature/ override; only then is auto-dispatch allowed."""
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
    manifest_cache: Mapping[tuple[Repo, Ref], Manifest],
    sync_branch: Ref | None,
    sync_exists_by_repo: Mapping[Repo, bool],
    can_dispatch: bool,
    lane: Execution,
    dispatch_plans: dict[tuple[Repo, Ref, Execution], DispatchPlan],
    pins: Mapping[Repo, Ref] | None = None,
) -> Literal["triggered rebuild"]:
    """Artifact missing, no producer CI in flight: raise, or (timing skew) record a DispatchPlan."""
    producer_manifest = manifest_cache.get((spec.repo, ref))
    if pins and spec.repo in pins:
        raise ResolveError(
            f"dep '{spec.package}' is pinned to {spec.repo}@{sha[:8]}, the commit under test, but "
            f"no artifact named '{artifact_name}' exists and no CI is in flight for it: "
            "that commit's CI did not publish this leg."
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

    if producer_manifest is not None:
        publishing = [k for k, pkgs in producer_manifest.packages_by_kind.items() if spec.package in pkgs]
        lanes = {producer_manifest.execution_by_kind[k] for k in publishing or producer_manifest.execution_by_kind}
        if lane not in lanes:
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
    pins: Mapping[Repo, Ref] | None = None,
    own_ref: Ref = Ref(""),
    own_compiler_inputs: Sequence[str] | None = None,
    local: Mapping[PackageName, ResolvedDep] | None = None,
) -> tuple[list[ResolvedDep], ResolvedOwn]:
    """Transitive deps (leaves first) and the OWN artifact of one matrix entry.

    `own_ref` is the commit's ref, for deps on packages of the own repo (siblings);
    `local` are siblings built earlier in the same job.
    """
    local = local or {}
    pins = pins or {}
    visited: dict[PackageName, ResolvedDep] = {}
    expanded: dict[PackageName, list[ResolvedDep]] = {}
    requests: dict[PackageName, tuple[str, dict[str, Any]]] = {}
    order: list[PackageName] = []

    def visit(spec: DepSpec, parent_ctx: Mapping[str, Any], declared_by: str, parent_ref: Ref) -> list[ResolvedDep]:
        """The artifact `spec` names, or the members of a meta package."""
        if spec.sibling and parent_ref == own_ref and spec.package in local:
            visited[spec.package] = local[spec.package]
            expanded[spec.package] = [local[spec.package]]
            if spec.package not in order:
                order.append(spec.package)
            return expanded[spec.package]
        if spec.sibling:
            ref = parent_ref
        elif spec.repo in pins:
            ref = pins[spec.repo]
        elif sync_branch and sync_exists_by_repo.get(spec.repo, False):
            ref = sync_branch
        else:
            ref = spec.ref
        producer = manifest_cache.get((spec.repo, ref))
        info = producer.packages.get(spec.package) if producer is not None else None

        compiler_inputs = spec.compiler_inputs
        if compiler_inputs is None and info is not None:
            compiler_inputs = info.compiler_inputs
        if compiler_inputs is None:
            raise ResolveError(
                f"dep '{spec.package}' (declared by '{declared_by}') takes its compiler-inputs from its producer, "
                f"but {spec.repo}@{ref} declares no package '{spec.package}'"
            )
        compiler = _join_compilers(compiler_inputs, parent_ctx, context=f"dep '{spec.package}'")

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

        # The ref, not its SHA: two lookups of one branch may see different commits.
        request = {
            "repo": spec.repo,
            "ref": ref,
            "compiler": compiler,
            "build-type": build_type,
            "platform": platform_slug,
            "python-version": python_version,
            "option": dep_option,
        }
        if spec.package in requests:
            first_by, first = requests[spec.package]
            differs = {k: (first[k], v) for k, v in request.items() if first[k] != v}
            if differs:
                detail = ", ".join(f"{k} {a!r} vs {b!r}" for k, (a, b) in differs.items())
                raise ResolveError(
                    f"dep '{spec.package}' is declared differently by '{first_by}' and '{declared_by}' "
                    f"({detail}). Declarations of the same package must agree."
                )
            return expanded[spec.package]
        requests[spec.package] = (declared_by, request)

        if info is not None and info.meta:
            ignored = [
                name
                for name, is_set in (
                    ("options", bool(spec.option)),
                    ("options-input", spec.options_input is not None),
                    ("build-type-input", spec.build_type_input != "build-type"),
                    ("platform-input", spec.platform_input != "platform"),
                    ("needs-python", spec.needs_python),
                    ("python-version-input", spec.python_version_input != "python-version"),
                )
                if is_set
            ]
            if ignored:
                raise ResolveError(
                    f"dep '{spec.package}' (declared by '{declared_by}') is a meta package of {spec.repo}: "
                    f"it has no artifact, and its members resolve against this leg, so {', '.join(ignored)} "
                    "would be ignored. Declare the member packages that need them instead."
                )
            members: list[ResolvedDep] = []
            for member in info.deps:
                if member.applies_to(parent_ctx, lane):
                    members += visit(member, parent_ctx, declared_by=spec.package, parent_ref=ref)
            expanded[spec.package] = _unique(members)
            return expanded[spec.package]

        # The leg the upstream's own CI built this variant on: sub-deps resolve and filter
        # by `when` against it, so deps-hash8 reproduces the published name.
        sub_specs = info.deps if info is not None else (producer.deps if producer is not None else [])
        requested = Variant(lane, platform_slug, compiler, build_type, python_version, dep_option)
        variants = producer_variants(producer, spec.package) if producer is not None else None
        if variants is None:
            # No matrix to tell (no manifest upstream): the requested variant over this leg stands in.
            dep_ctx = {**parent_ctx, "build-type": build_type, "platform": platform, "options": dep_option}
            if python_version is not None:
                dep_ctx["python-version"] = python_version
        else:
            assert producer is not None
            own_leg = producer_leg(producer, spec.package, requested, sub_specs)
            if own_leg is None:
                published = "".join(f"\n  {v}" for v in variants) or " none"
                raise ResolveError(
                    f"dep '{spec.package}' (declared by '{declared_by}') from {spec.repo}@{ref} is requested as "
                    f"{requested}, which no leg of the producer's manifest publishes, so it can never be built. "
                    f"The producer publishes:{published}\n"
                    "Add the leg to the producer, or change what this consumer requests."
                )
            dep_ctx = dict(own_leg)

        sub_deps: list[ResolvedDep] = []
        for sub_spec in sub_specs:
            if sub_spec.applies_to(dep_ctx, lane):
                sub_deps += visit(sub_spec, dep_ctx, declared_by=spec.package, parent_ref=ref)

        sha_key = (spec.repo, ref)
        if sha_key not in sha_cache:
            sha_cache[sha_key] = Sha(ref) if spec.repo in pins else resolve_ref_to_sha(spec.repo, ref, token)
        sha = sha_cache[sha_key]

        deps_hash8 = compute_deps_hash8([d.artifact_name for d in _unique(sub_deps)])

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
                source = _classify_orphan_pin(
                    spec=spec,
                    ref=ref,
                    sha=sha,
                    artifact_name=artifact_name,
                    manifest_cache=manifest_cache,
                    sync_branch=sync_branch,
                    sync_exists_by_repo=sync_exists_by_repo,
                    can_dispatch=can_dispatch,
                    lane=lane,
                    dispatch_plans=dispatch_plans,
                    pins=pins,
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
            add_to_path=tuple(info.add_to_path) if info is not None else (),
        )
        visited[spec.package] = resolved
        expanded[spec.package] = [resolved]
        order.append(spec.package)
        return [resolved]

    direct: list[ResolvedDep] = []
    for spec in own_deps:
        if spec.applies_to(matrix_entry, lane):
            direct += visit(spec, matrix_entry, declared_by=own.name, parent_ref=own_ref)
    direct = _unique(direct)

    deps_resolved = [visited[name] for name in order]

    direct_dep_artifact_names = [d.artifact_name for d in direct]
    own_compiler = _join_compilers(
        own.compiler_inputs if own_compiler_inputs is None else own_compiler_inputs,
        matrix_entry,
        context=f"[package] '{own.name}'",
    )
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
        direct_artifact_names=tuple(direct_dep_artifact_names),
    )


def resolve_packages(
    manifest: Manifest,
    prefixes: Sequence[PackageName],
    *,
    own_sha: Sha,
    own_ref: Ref,
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
    pins: Mapping[Repo, Ref] | None = None,
) -> tuple[list[ResolvedDep], dict[PackageName, ResolvedOwn]]:
    """One leg of a kind publishing `prefixes` in build order: the deps to fetch, and each package's artifact."""
    built: dict[PackageName, ResolvedDep] = {}
    per_package: dict[PackageName, ResolvedOwn] = {}
    to_fetch: dict[PackageName, ResolvedDep] = {}
    primary = manifest.packages[manifest.package.prefix]
    for prefix in prefixes:
        info = manifest.packages.get(prefix, primary)
        deps, own = resolve_leg(
            own=manifest.package,
            own_deps=info.deps,
            own_sha=own_sha,
            matrix_entry=matrix_entry,
            manifest_cache=manifest_cache,
            sync_branch=sync_branch,
            sync_exists_by_repo=sync_exists_by_repo,
            sha_cache=sha_cache,
            artifact_cache=artifact_cache,
            run_state_cache=run_state_cache,
            token=token,
            can_dispatch=can_dispatch,
            lane=lane,
            dispatch_plans=dispatch_plans,
            own_prefix_override=None if prefix == manifest.package.prefix else prefix,
            pins=pins,
            own_ref=own_ref,
            own_compiler_inputs=info.compiler_inputs,
            local=built,
        )
        for d in deps:
            if d.source != "local":
                to_fetch.setdefault(d.name, d)
        per_package[prefix] = own
        built[prefix] = _as_local(prefix, manifest.package.repo, own_ref, own_sha, own)
    return list(to_fetch.values()), per_package


def build_order(manifest: Manifest, prefixes: Sequence[PackageName]) -> list[PackageName]:
    """`prefixes` with every package after the packages of this repo it depends on."""
    wanted = set(prefixes)
    out: list[PackageName] = []

    def place(p: PackageName) -> None:
        if p in out:
            return
        for d in manifest.packages[p].deps:
            if d.sibling and d.package in wanted:
                place(d.package)
        out.append(p)

    for p in prefixes:
        place(p)
    return out


def _as_local(prefix: PackageName, repo: Repo, ref: Ref, sha: Sha, own: ResolvedOwn) -> ResolvedDep:
    """A package built earlier in the same job, as its siblings see it."""
    return ResolvedDep(
        name=prefix,
        repo=repo,
        ref=ref,
        sha=sha,
        artifact_name=own.artifact_name,
        cached=False,
        source="local",
        needs_python=False,
        install_path=_install_base() / prefix,
        platform=own.platform,
        compiler=own.compiler,
        build_type=own.build_type,
        python_version=own.python_version,
        deps_hash=own.deps_hash,
    )


def _unique(deps: Sequence[ResolvedDep]) -> list[ResolvedDep]:
    """First occurrence wins; a package reached through two members appears once."""
    out: dict[PackageName, ResolvedDep] = {}
    for d in deps:
        out.setdefault(d.name, d)
    return list(out.values())


def bfs_load_manifests(
    root_deps: Sequence[DepSpec],
    sync_branch: Ref | None,
    token: str | None,
    manifest_path: str,
    max_depth: int = 8,
    pins: Mapping[Repo, Ref] | None = None,
) -> tuple[dict[tuple[Repo, Ref], Manifest], dict[Repo, bool]]:
    """Returns (manifests, sync_branch exists per repo)."""
    pins = pins or {}
    manifest_cache: dict[tuple[Repo, Ref], Manifest] = {}
    sync_exists: dict[Repo, bool] = {}
    queue: list[tuple[Repo, Ref]] = [(d.repo, pins.get(d.repo, d.ref)) for d in root_deps]

    for _depth in range(max_depth):
        layer = [(r, ref) for (r, ref) in queue if (r, ref) not in manifest_cache]
        if not layer:
            break

        results = fetch_manifests_layer(layer, sync_branch, token, manifest_path)

        next_queue: list[tuple[Repo, Ref]] = []
        for (raw_repo, raw_ref), (text, sync_present) in results.items():
            repo, ref = Repo(raw_repo), Ref(raw_ref)
            if sync_branch and repo not in pins:
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
            for sub in m.external_deps():
                next_queue.append((sub.repo, pins.get(sub.repo, sub.ref)))

        queue = next_queue

    return manifest_cache, sync_exists


@click.command(help="Resolve dep tree for all matrix legs in a manifest.")
@click.option("--manifest", default=".ci/manifest.toml", help="Path to local manifest TOML")
@click.option(
    "--current-branch",
    "current_branch",
    default="",
    help="Branch being built; a sync-branch/ or feature/ branch is used where an upstream has it too",
)
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
@click.option(
    "--pin",
    default="",
    help="owner/repo@sha: resolve that repo at this commit wherever it appears (the change under test).",
)
def main(
    manifest: str,
    current_branch: str,
    matrix: str,
    self_repo: str,
    upstream_manifest_path: str,
    pin: str,
) -> None:
    try:
        _run(manifest, current_branch, matrix, self_repo, upstream_manifest_path, pin)
    except (ResolveError, ValueError) as e:
        raise CIError(str(e)) from e


def _run(
    manifest: str,
    current_branch: str,
    matrix: str,
    self_repo: str,
    upstream_manifest_path: str,
    pin: str = "",
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

    pins = parse_pin(pin)
    token = select_token()

    own_sha = _resolve_own_sha(str(local_manifest.package.repo), current_branch, token)

    manifest_cache, sync_exists = bfs_load_manifests(
        root_deps=local_manifest.external_deps(),
        sync_branch=sync_branch,
        token=token,
        manifest_path=upstream_manifest_path,
        pins=pins,
    )

    own_ref = Ref(current_branch)
    own_repo = local_manifest.package.repo
    # Packages of this repo resolve against this commit.
    manifest_cache[(own_repo, own_ref)] = local_manifest
    sha_cache: dict[tuple[Repo, Ref], Sha] = {(own_repo, own_ref): own_sha}
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
        built = build_order(local_manifest, local_manifest.built_by_kind.get(mname, ()))
        prefixes = built or [local_manifest.package.prefix]
        lane = local_manifest.execution_by_kind.get(mname, EXECUTION_RUNNER)
        package = local_manifest.packages.get(prefixes[0], local_manifest.packages[local_manifest.package.prefix])
        for entry in include:
            deps_resolved, per_package = resolve_packages(
                local_manifest,
                prefixes,
                own_sha=own_sha,
                own_ref=own_ref,
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
                pins=pins,
            )
            own = per_package[prefixes[0]]
            cmake_paths = [str(d.install_path) for d in deps_resolved]
            all_artifact_names = [d.artifact_name for d in deps_resolved]
            resolved_block: dict[str, Any] = {
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
                "direct-artifact-names": " ".join(own.direct_artifact_names),
                "job-name": job_names.name_suffix(entry, include, package.compiler_inputs),
            }
            resolved_block["packages"] = {
                p: {
                    "own-artifact-name": o.artifact_name,
                    "own-deps-hash": o.deps_hash or "",
                    "direct-artifact-names": " ".join(o.direct_artifact_names),
                    "install-path": str(_install_base() / p),
                }
                for p, o in per_package.items()
            }
            out_include.append({**entry, "_resolved": resolved_block})

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
        print(f"'{sync_branch}' also used in: {', '.join(sync_repos) if sync_repos else '(none)'}")


if __name__ == "__main__":
    main()
