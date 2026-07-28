#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
resolve_deps.py

Reads a local .ci/manifest.toml, walks the upstream dependency graph by fetching
each dep's manifest from GitHub, and emits a fully resolved dependency tree as
JSON. Designed to run once per workflow (in a 'resolve' job) so downstream
build/test/clang-tidy jobs can consume the same resolved tree without
re-querying GitHub per matrix leg.

Manifest schema (.ci/manifest.toml in each consumer repo):

    [package]
    name   = "cxxmath"          # human-readable
    prefix = "cxxmath"          # artifact-name prefix
    repo   = "owner/repo"       # this repo's owner/name (for the resolve job)
    compiler-inputs = ["cxx-compiler"]      # required: matrix fields whose values
                                            # identify the OWN artifact. [] for
                                            # pure-Python repos with no compiled artifact.

    [[deps]]
    repo            = "owner/upstream"
    package         = "fortmath"            # the upstream's prefix
    ref             = "main"                # required: branch / tag / 40-char SHA.
                                            # Sync-branch override still applies.
    compiler-inputs = ["fortran-compiler"]  # required: matrix fields whose values
                                            # identify this dep's artifact. Must match
                                            # the upstream's [package].compiler-inputs.
                                            # [] for compiler-independent deps (e.g.
                                            # ecbuild, header-only / pure-data packages).
    build-type-input  = "build-type"        # default
    platform-input    = "platform"          # default
    needs-python      = false               # default

    [[matrix.build.include]]
    cxx-compiler        = "clang++-18"     # binary; passed straight to CMake
    fortran-compiler    = "gfortran-14"    # binary; passed straight to CMake
    build-type          = "Release"
    platform            = "ubuntu-24.04"   # REQUIRED: explicit binary-compatibility class
    runs-on            = "arc-sandbox-cci2"  # scheduling only — not part of artifact identity
    container           = "registry.example/playground-ci/ubuntu24.04-clang18-gfortran13:latest"  # scheduling only

`platform` is REQUIRED on every matrix leg and is used verbatim as the artifact-name
platform slot. `runs-on` and `container` are pure scheduling (which runner / image
delivers the tools) and never enter artifact identity. Declaring the same `platform`
across legs lets several ABI-compatible images (and a same-distro host runner) share one
artifact: a producer built under one image is reused under another instead of rebuilt.
Because the image tag no longer enters the slug, you own cache invalidation — bump the
platform string when the ABI changes within a distro release.

The compiler segment of an artifact name is built by sorting the listed compiler-inputs
field names alphabetically and joining their values with '-'. So upstream and downstream
need to agree only on the *set* of compiler fields, not the order they declare them.

Outputs (key=value to $GITHUB_OUTPUT, or stdout for debug):

    matrix-<name>=<JSON: {"include": [...]}>
        One per requested matrix block (e.g. matrix-build, matrix-clang-tidy).
        Same structure GitHub Actions consumes via fromJSON. Each entry has the
        original matrix fields plus a '_resolved' object with the resolved
        per-leg dep tree:
          _resolved.cmake-prefix-path     semicolon-separated install paths
          _resolved.all-artifact-names    space-separated transitive artifact names
          _resolved.own-artifact-name     this package's own artifact name for this leg
          _resolved.deps                  list of {name, repo, ref, sha, artifact-name,
                                                   source, needs-python, install-path}

    json=<JSON: full output>
        All blocks together, keyed by matrix name. Useful for diagnostics.

GitHub API access: uses 'gh api' (REST + GraphQL) so the same auth flow as
existing scripts. Set GH_TOKEN to the desired token. One GraphQL call per BFS
layer fetches all newly discovered manifests + sync-branch ref existence in
parallel.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, NewType, cast

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import click

from . import s3_store
from ._errors import CIError
from ._github_api import (
    IN_PROGRESS_STATUSES,
    canonical_option_segment,
    compute_deps_hash8,
    compute_platform_slug,
    gh_api_rest,
    select_token,
)
from ._github_api import fetch_manifests_layer as _fetch_manifests_layer_str

# Discriminating fields that identify a buildable leg — the inputs to the
# artifact name. `platform` is the binary-compatibility class; `runs-on` and
# `container` are pure scheduling (which runner / image delivers the tools) and
# never enter artifact identity, so they are deliberately absent here. Any of
# these a consumer's matrix entry sets must match a producer leg verbatim,
# otherwise it's a misconfiguration the producer can't satisfy.
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

_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SYNC_BRANCH_RE: Final = re.compile(r"^(?:sync-branch-|feature-sync-)")
_OPTION_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


def _as_option(raw: Any, context: str) -> str:
    """Coerce + validate a build-option value (from a matrix leg or [[deps]]).

    Options are a scalar named configuration: a single string naming the whole
    feature config (e.g. 'stochastic-moments', or a curated combo like
    'moments-fast'), or empty/absent (-> '') for a plain build. A list is rejected
    with a migration hint — the composable-set form was replaced by scalar named
    configs so each maps 1:1 onto a CMake preset. Rejects any name outside
    [A-Za-z0-9_-] so it can't corrupt the artifact-name segment.
    """
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


Repo = NewType("Repo", str)  # "owner/name"
PackageName = NewType("PackageName", str)  # the [package].prefix string
Ref = NewType("Ref", str)  # branch / tag / SHA the user wrote
Sha = NewType("Sha", str)  # 40-char commit SHA returned by the API
ArtifactName = NewType("ArtifactName", str)


def as_repo(s: str) -> Repo:
    if "/" not in s or s.count("/") != 1 or not all(s.split("/", 1)):
        raise ValueError(f"repo must be 'owner/name', got {s!r}")
    return Repo(s)


def as_sha(s: str) -> Sha:
    if not _SHA_RE.fullmatch(s):
        raise ValueError(f"not a 40-char commit SHA: {s!r}")
    return Sha(s)


class ResolveError(Exception):
    """Resolver gave up. main() catches this, prints ::error::, and exits 1."""


@dataclass(frozen=True)
class DepSpec:
    """A dep entry from a manifest, before resolution."""

    repo: Repo
    package: PackageName
    ref: Ref  # required: branch, tag, or 40-char SHA
    compiler_inputs: Sequence[str]  # required: matrix fields whose values identify this dep's artifact
    build_type_input: str
    platform_input: str
    needs_python: bool
    python_version_input: str
    # Which upstream build OPTION config to consume. Options are an orthogonal,
    # per-package axis that does NOT propagate by default (unlike build-type): a dep
    # is consumed with no option unless asked. `option` is a fixed literal;
    # `options_input` reads the scalar config name from a named matrix-leg field
    # (per-leg selection). Both empty -> consume the plain (no-option) build.
    option: str = ""
    options_input: str | None = None


@dataclass(frozen=True)
class PackageSpec:
    """A manifest's [package] block."""

    name: str
    prefix: PackageName
    repo: Repo
    compiler_inputs: Sequence[str]  # required: matrix fields whose values identify the OWN artifact
    needs_python: bool


@dataclass
class Manifest:
    package: PackageSpec
    deps: list[DepSpec] = field(default_factory=list)
    matrix: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Per-kind override of the artifact-name prefix this kind publishes.
    # None for a kind == use package.prefix (the common case). Set only when a
    # kind publishes a SECONDARY artifact in the same repo (e.g. ecflow's
    # build-python publishes `ecflowmath-python-*`, distinct from build's
    # `ecflowmath-*`). The generator validates the value; we just consume it.
    artifact_prefix_by_kind: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedDep:
    """A fully resolved dep for one matrix leg."""

    name: PackageName
    repo: Repo
    ref: Ref  # the ref we ended up using (sync-branch or pinned)
    sha: Sha
    artifact_name: ArtifactName
    cached: bool  # True when the artifact is already in the S3 store at resolve time
    # artifact          — comes from the artifact store. Either it was already
    #                     there at resolve time, or the producer's CI is racing
    #                     this resolve and fetch_deps will poll for its upload.
    #                     Either way we took no corrective action.
    # triggered rebuild — orphan pin classified as timing skew; _run dispatches
    #                     the producer's cross-repo-trigger.yml before exiting so
    #                     fetch_deps later sees an in-flight run.
    source: Literal["artifact", "triggered rebuild"]
    needs_python: bool
    install_path: Path  # canonical install path
    # The structured fields that make up artifact_name. Carried explicitly so
    # consumers (e.g. the dependency table) read them directly instead of
    # reverse-parsing the name, which is ambiguous once optional segments
    # (deps-hash / compiler / python) are dropped.
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
    """The OWN package's artifact name plus the structured fields that compose it.

    Carried explicitly (rather than re-parsed from the name) so the dependency
    table can render the OWN row through the same structured path as the deps.
    """

    artifact_name: ArtifactName
    platform: str
    compiler: str | None
    build_type: str
    python_version: str | None
    deps_hash: str | None


@dataclass(frozen=True)
class DispatchPlan:
    """A producer that needs its cross-repo-trigger.yml fired before fetch_deps runs."""

    repo: Repo
    ref: Ref
    sha: Sha


def _parse_compiler_inputs(raw: object, context: str, allow_empty: bool) -> list[str]:
    """Validate and normalize a 'compiler-inputs' field.

    Required to be a list of non-empty strings naming matrix fields. allow_empty=True
    permits [] for both the OWN package (pure-Python repos) and [[deps]] entries
    (compiler-independent upstreams such as ecbuild). A dep with [] resolves to an
    artifact name with no compiler segment (see _join_compilers).
    """
    if raw is None:
        raise ValueError(
            f"{context} must declare 'compiler-inputs' as a list of matrix field names "
            '(e.g. ["cxx-compiler"] or ["cxx-compiler", "fortran-compiler"]'
            + (" or [] for an uncompiled package)." if allow_empty else ").")
        )
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{context}: 'compiler-inputs' must be a list of strings, got {raw!r}")
    cleaned = [s.strip() for s in raw]
    if any(not s for s in cleaned):
        raise ValueError(f"{context}: 'compiler-inputs' contains an empty entry: {raw!r}")
    if not cleaned and not allow_empty:
        raise ValueError(
            f"{context}: 'compiler-inputs' must be non-empty — every dep we fetch from upstream "
            "has at least one compiler in its artifact name."
        )
    return cleaned


def _parse_package(data: Mapping[str, Any], default_repo: str | None) -> PackageSpec:
    pkg_data = data.get("package", {})
    if not pkg_data.get("name") or not pkg_data.get("prefix"):
        raise ValueError("manifest [package] must define both 'name' and 'prefix'")
    repo = pkg_data.get("repo") or default_repo
    if not repo:
        raise ValueError("manifest [package] must define 'repo' (or pass --self-repo)")

    pkg_compiler_inputs = _parse_compiler_inputs(
        pkg_data.get("compiler-inputs"),
        context=f"[package] '{pkg_data['name']}'",
        allow_empty=True,
    )

    return PackageSpec(
        name=str(pkg_data["name"]),
        prefix=PackageName(str(pkg_data["prefix"])),
        repo=as_repo(str(repo)),
        compiler_inputs=pkg_compiler_inputs,
        needs_python=bool(pkg_data.get("needs-python", False)),
    )


def _parse_deps(data: Mapping[str, Any]) -> list[DepSpec]:
    deps: list[DepSpec] = []
    for raw in data.get("deps", []):
        if "repo" not in raw or "package" not in raw:
            raise ValueError(f"each [[deps]] entry must define 'repo' and 'package': got {raw!r}")
        if "ref" not in raw or not str(raw["ref"]).strip():
            raise ValueError(
                f"[[deps]] entry for package '{raw['package']}' must declare 'ref' "
                '(e.g. "main", "master", a tag, or a 40-char SHA).'
            )
        compiler_inputs = _parse_compiler_inputs(
            raw.get("compiler-inputs"),
            context=f"[[deps]] entry for package '{raw['package']}'",
            allow_empty=True,
        )
        option_literal = _as_option(raw.get("options"), context=f"[[deps]] entry for package '{raw['package']}'")
        options_input = raw.get("options-input")
        deps.append(
            DepSpec(
                repo=as_repo(str(raw["repo"])),
                package=PackageName(str(raw["package"])),
                ref=Ref(str(raw["ref"]).strip()),
                compiler_inputs=compiler_inputs,
                build_type_input=str(raw.get("build-type-input", "build-type")),
                platform_input=str(raw.get("platform-input", "platform")),
                needs_python=bool(raw.get("needs-python", False)),
                python_version_input=str(raw.get("python-version-input", "python-version")),
                option=option_literal,
                options_input=str(options_input) if options_input is not None else None,
            )
        )
    return deps


def _parse_matrix(data: Mapping[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    # Two-pass matrix resolution: first collect raw blocks, then apply reuse-matrix.
    # Mirrors generate_downstream_ci.py: a kind with `reuse-matrix = "X"` and no
    # explicit include inherits X's include legs. Without this expansion, kinds
    # like `[matrix.test] reuse-matrix = "build"` would emit an empty matrix and
    # the consuming workflow would fail with `fromJSON: empty input`.
    raw_matrix: dict[str, dict[str, Any]] = {}
    for job_name, job_block in data.get("matrix", {}).items():
        if not isinstance(job_block, dict):
            raise ValueError(f"[matrix.{job_name}] must be a table")
        raw_matrix[str(job_name)] = job_block

    matrix: dict[str, list[dict[str, Any]]] = {}
    artifact_prefix_by_kind: dict[str, str] = {}
    for job_name, job_block in raw_matrix.items():
        include = job_block.get("include")
        reuse = job_block.get("reuse-matrix")
        if reuse is not None and include:
            raise ValueError(f"[matrix.{job_name}] sets both 'reuse-matrix' and 'include'; pick one")
        if reuse is not None:
            target = raw_matrix.get(str(reuse))
            if target is None:
                raise ValueError(f"[matrix.{job_name}].reuse-matrix = {reuse!r} but [matrix.{reuse}] does not exist")
            if target.get("reuse-matrix") is not None:
                raise ValueError(
                    f"[matrix.{job_name}].reuse-matrix points at another reuse-matrix; chained reuse is not supported"
                )
            include = target.get("include", [])
        else:
            include = include or []
        if not isinstance(include, list):
            raise ValueError(f"[matrix.{job_name}.include] must be an array of tables")
        matrix[str(job_name)] = [dict(entry) for entry in include]
        prefix_override = job_block.get("artifact-prefix")
        if prefix_override is not None:
            if not isinstance(prefix_override, str) or not prefix_override.strip():
                raise ValueError(f"[matrix.{job_name}].artifact-prefix must be a non-empty string")
            artifact_prefix_by_kind[str(job_name)] = prefix_override.strip()

    return matrix, artifact_prefix_by_kind


def parse_manifest(text: str, default_repo: str | None = None) -> Manifest:
    """Parse a TOML manifest string into a Manifest."""
    data = tomllib.loads(text)
    package = _parse_package(data, default_repo)
    deps = _parse_deps(data)
    matrix, artifact_prefix_by_kind = _parse_matrix(data)
    return Manifest(
        package=package,
        deps=deps,
        matrix=matrix,
        artifact_prefix_by_kind=artifact_prefix_by_kind,
    )


def fetch_manifests_layer(
    repos_refs: Sequence[tuple[Repo, Ref]],
    sync_branch: Ref | None,
    token: str | None,
    manifest_path: str,
) -> dict[tuple[Repo, Ref], tuple[str | None, bool]]:
    """Typed wrapper around _github_api.fetch_manifests_layer.

    The shared helper takes plain `str` so the generator (which has no
    Repo/Ref NewType vocabulary) can also use it. resolve_deps wraps the
    return value back into its NewType-keyed dict.
    """
    raw = _fetch_manifests_layer_str(repos_refs, sync_branch, token, manifest_path)
    return cast("dict[tuple[Repo, Ref], tuple[str | None, bool]]", raw)


def resolve_ref_to_sha(repo: Repo, ref: Ref, token: str | None) -> Sha:
    """Resolve a branch / lightweight or annotated tag / short or full SHA to a 40-char commit SHA.

    Uses the commits/{ref} REST endpoint, which auto-disambiguates and dereferences
    annotated tags to the underlying commit (the tags/{ref} endpoint we used previously
    returned the tag-object SHA for annotated tags, which doesn't match how artifacts
    are keyed). Mirrored in check_artifact.resolve_ref.
    """
    if _SHA_RE.fullmatch(ref):
        return Sha(ref)
    data = gh_api_rest(f"repos/{repo}/commits/{ref}", token)
    if isinstance(data, dict) and isinstance(data.get("sha"), str):
        return as_sha(cast(str, data["sha"]))
    raise ResolveError(f"Could not resolve ref '{ref}' in {repo}")


def _resolve_own_sha(own_repo: str, current_branch: str, token: str | None) -> Sha:
    """Resolve this package's own artifact sha from the branch being built.

    Every consumer derives an upstream artifact's identity via
    resolve_ref_to_sha(repo, ref) — the branch-head sha — so the producer must
    name what it publishes the SAME way. We deliberately never fall back to
    GITHUB_SHA: on pull_request events it is the synthetic merge commit
    (refs/pull/N/merge), which no consumer can resolve. Naming our artifact after
    it makes downstream jobs poll forever for a name we never publish.
    Push-to-main is unchanged, since there the branch head IS GITHUB_SHA.
    """
    if not current_branch:
        raise ResolveError("Cannot determine own artifact SHA: --current-branch is required")
    return resolve_ref_to_sha(Repo(own_repo), Ref(current_branch), token)


def has_in_flight_run(repo: Repo, sha: Sha, token: str | None) -> bool:
    """True if at least one workflow run for this commit SHA is queued / in progress.

    Used to distinguish orphan pins (no upstream CI ever ran for this commit, or
    every run completed without producing the artifact we need) from coordinated
    pushes (upstream CI is racing this resolve job and will produce the artifact
    soon — fetch_deps polls in that case).
    """
    data = gh_api_rest(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=20", token)
    if not isinstance(data, dict):
        return False
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return False
    for run in runs:
        if isinstance(run, dict) and run.get("status") in IN_PROGRESS_STATUSES:
            return True
    return False


def producer_can_build(producer_manifest: Manifest, matrix_entry: Mapping[str, Any]) -> bool:
    """True if any [matrix.<kind>.include] leg in the producer matches the
    consumer's request on every discriminator BOTH the producer leg and the
    consumer declare.

    A producer only "discriminates on" the fields its own matrix legs name —
    those are what land in its artifact name. Consumer fields the producer
    doesn't declare are irrelevant: an ecbuild leg `{build-type, platform}`
    can satisfy a fortmath consumer asking for `{..., fortran-compiler:
    gfortran-13}` because fortran-compiler doesn't enter ecbuild's artifact
    name. We match on the intersection so that compiler-independent producers
    (compiler-inputs = []) don't false-fail against compiler-specific
    consumers. Note that `runs-on`/`container` are not discriminators (see
    _MATRIX_DISCRIMINATORS): they are scheduling, not identity, so two
    ABI-compatible images on the same `platform` match.

    Producer kinds are walked union-style: any kind's include legs covering
    the requested combo are sufficient. An empty producer matrix is treated
    as buildable — the resolver has no evidence to fail on.
    """
    if not producer_manifest.matrix:
        return True
    # `options` is matched explicitly below: unlike the scalar discriminators
    # (where a producer leg that omits a field simply doesn't discriminate on
    # it), an omitted `options` means the concrete empty config, so it must equal
    # the requested config — a plain leg cannot satisfy a moments request.
    consumer_keys = (_MATRIX_DISCRIMINATORS - {"options"}) & matrix_entry.keys()
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
    """True iff `ref` is the dep's declared ref or a valid sync-branch override.

    Distinguishes timing skew (producer needs a rebuild on its declared ref) from
    intentional divergence (consumer pinned a non-default branch / tag / SHA that
    the producer's CI never built). We only auto-dispatch in the timing-skew case;
    the divergence case fails loudly so the consumer fixes its pin.
    """
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
    """Fire a workflow_dispatch into the producer's cross-repo-trigger.yml,
    then poll the producer's recent runs for ~60s until the new run appears.

    The appearance wait is load-bearing: without it, fetch_deps.py's
    poll_for_artifact would query upstream_run_status and possibly see "none"
    before GitHub registers our dispatched run, and bail out with a misleading
    diagnostic instead of polling.

    Raises ResolveError if `gh workflow run` exits non-zero. A timed-out
    appearance wait emits a ::warning:: but doesn't fail — fetch_deps may still
    catch the run a few seconds later.
    """
    dispatch_id = (
        f"resolve-{os.environ.get('GITHUB_RUN_ID', '0')}-"
        f"{os.environ.get('GITHUB_RUN_ATTEMPT', '0')}-{secrets.token_hex(4)}"
    )
    cmd = [
        "gh",
        "workflow",
        "run",
        "cross-repo-trigger.yml",
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
        # Mutually exclusive with from-job: the producer's per-kind filter
        # opts into this trigger via `triggers = ["rebuild-request", ...]`.
        "-f",
        "rebuild-request=true",
        "-f",
        f"branch={branch}",
        "-f",
        f"fallback-ref={fallback_ref}",
    ]
    env = {**os.environ, "GH_TOKEN": token}
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise ResolveError(
            f"Failed to dispatch cross-repo-trigger.yml in {plan.repo}@{plan.ref} "
            f"(dispatcher {dispatcher_repo}@{dispatcher_sha[:8]}): {result.stderr.strip()}"
        )

    print(
        f"::notice::Triggering REBUILD of upstream {plan.repo}@{plan.ref} (sha={plan.sha[:8]}): its "
        "artifact was missing, so it will recompile. Downstream build jobs will WAIT for it to finish."
    )
    print(
        f"  dispatched {plan.repo}@{plan.ref} (sha={plan.sha[:8]}, dispatch-id={dispatch_id}); "
        "waiting for run to appear"
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if has_in_flight_run(plan.repo, plan.sha, token):
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
) -> ArtifactName:
    """Mirrors the naming logic in check_artifact.py exactly so cache keys still hit.

    None for deps_hash8 / compiler / python_version means "this segment doesn't
    apply" (no deps, no compilers declared, not a Python build) — those segments
    are dropped from the name, matching the pre-Optional `if deps_hash8:` etc.
    behaviour exactly.

    `option` is the orthogonal build-option axis (a scalar config name); its segment
    is appended only when non-empty (empty -> name unchanged from the pre-options
    format). See canonical_option_segment.
    """
    parts = [prefix, sha]
    if deps_hash8:
        parts.append(deps_hash8)
    parts.append(platform_slug)
    if compiler:
        parts.append(compiler)
    if python_version:
        parts.append(f"py{python_version}")
    parts.append(build_type)
    opt_seg = canonical_option_segment(option)
    if opt_seg:
        parts.append(opt_seg)
    return ArtifactName("-".join(parts))


def _install_base() -> Path:
    """Mirror actions/install-prefix: $RUNNER_TEMP/install.

    Returns a literal Path containing the env-var reference, NOT an
    interpolated absolute path. The resolver runs in one runner (e.g. ARC
    with RUNNER_TEMP=/__w/_temp) while downstream build/test jobs may run
    in a different one (e.g. ubuntu-latest host with RUNNER_TEMP=/home/
    runner/work/_temp) — baking the resolver's RUNNER_TEMP into the
    matrix output makes the install path invalid on the consumer side.
    Consumers (fetch_deps, fetch-and-publish's compose-cmake) call
    os.path.expandvars at runtime to get a real absolute path.
    """
    return Path("$RUNNER_TEMP") / "install"


def _join_compilers(
    compiler_inputs: Sequence[str],
    matrix_entry: Mapping[str, Any],
    context: str,
) -> str | None:
    """Look up each named field in matrix_entry, join values with '-' in alphabetical
    field-name order. Empty `compiler_inputs` → None (no compiler segment to add).
    Failure modes (field missing, field value empty) raise ResolveError.
    """
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
    dispatch_plans: dict[tuple[Repo, Ref], DispatchPlan],
) -> Literal["triggered rebuild"]:
    """Triage an orphan pin: artifact missing AND no producer CI in flight.

    Three-way decision (any non-timing-skew case raises):

      1. Matrix mismatch — producer's manifest declares no leg matching the
         requested (os, compiler, build-type, python-version) tuple. The
         consumer asked for a build the producer can't make. Hard fail.
      2. Ref divergence — consumer pinned a non-default ref the producer's CI
         never built. Auto-rebuilding wouldn't restore the cached artifact
         under that ref (and the consumer probably means it). Hard fail.
      3. Timing skew — same ref, supported combo, just no current build.
         Record a DispatchPlan so _run can dispatch the producer's
         cross-repo-trigger.yml after all legs resolve. The dep's source becomes
         "triggered rebuild"; once the dispatch fires, fetch_deps' poll path
         takes over.

    can_dispatch=False (no DISPATCH_TOKEN configured) folds the timing-skew
    case into a hard fail with the legacy diagnostic — preserves the
    pre-recovery behaviour for repos without App credentials.
    """
    producer_manifest = manifest_cache.get((spec.repo, ref))
    if producer_manifest is not None and not producer_can_build(producer_manifest, matrix_entry):
        # Surface only the fields the producer actually discriminates on (i.e.
        # appear in at least one of its legs). Those are the ones the consumer
        # has to negotiate; anything else is irrelevant to whether the producer
        # can build this leg.
        producer_keys = set().union(*(leg.keys() for legs in producer_manifest.matrix.values() for leg in legs))
        relevant_keys = _MATRIX_DISCRIMINATORS & matrix_entry.keys() & producer_keys
        relevant = {k: matrix_entry[k] for k in sorted(relevant_keys)}
        # options is matched even when no producer leg names it (absence == ""),
        # so surface the requested config explicitly rather than via producer_keys.
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

    dispatch_plans.setdefault((spec.repo, ref), DispatchPlan(repo=spec.repo, ref=ref, sha=sha))
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
    dispatch_plans: dict[tuple[Repo, Ref], DispatchPlan],
    own_prefix_override: str | None = None,
) -> tuple[list[ResolvedDep], ResolvedOwn]:
    """
    Resolve the full transitive dep set for one matrix entry.

    Returns (deps_in_dependency_order, own).
    deps_in_dependency_order has leaves first, parents later — matches the order
    callers want for cmake-prefix-path concatenation. `own` carries the OWN
    package's artifact name and its structured fields.
    """
    # DFS post-order so leaves come first.
    visited: dict[PackageName, ResolvedDep] = {}
    order: list[PackageName] = []

    def visit(spec: DepSpec, parent_ctx: Mapping[str, Any]) -> ResolvedDep:
        if spec.package in visited:
            return visited[spec.package]

        compiler = _join_compilers(spec.compiler_inputs, parent_ctx, context=f"dep '{spec.package}'")

        # Default is sensible for build-type, so .get is fine here. platform is
        # required — compute_platform_slug raises if the leg didn't declare it.
        build_type = str(parent_ctx.get(spec.build_type_input, "Release"))
        platform = str(parent_ctx.get(spec.platform_input, ""))
        platform_slug = compute_platform_slug(platform)
        python_version: str | None = str(parent_ctx.get(spec.python_version_input, "")) if spec.needs_python else None

        # Options do NOT propagate by default: a fixed literal wins, else read
        # the per-leg field named by options-input, else consume the plain build.
        if spec.option:
            dep_option = spec.option
        elif spec.options_input is not None:
            dep_option = _as_option(parent_ctx.get(spec.options_input), context=f"dep '{spec.package}'")
        else:
            dep_option = ""

        # Apply sync-branch override.
        ref = sync_branch if sync_branch and sync_exists_by_repo.get(spec.repo, False) else spec.ref

        # Recurse into the dep's own deps first so deps-hash includes their artifact names.
        # Pass parent_ctx unchanged: the dep's own deps look up THEIR compiler-input field
        # against the same matrix entry, so propagation works without rewriting keys.
        sub_deps: list[ResolvedDep] = []
        sub_manifest = manifest_cache.get((spec.repo, ref))
        if sub_manifest is not None:
            for sub_spec in sub_manifest.deps:
                sub_deps.append(visit(sub_spec, parent_ctx))

        # Resolve SHA (via REST — one call per unique (repo, ref)).
        sha_key = (spec.repo, ref)
        if sha_key not in sha_cache:
            sha_cache[sha_key] = resolve_ref_to_sha(spec.repo, ref, token)
        sha = sha_cache[sha_key]

        # Compute deps-hash8 from this dep's own direct compiled deps.
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
        )

        # Look up artifact (cache by name). The store is keyed purely by
        # artifact name, so the producer repo never enters the lookup.
        if artifact_name not in artifact_cache:
            artifact_cache[artifact_name] = s3_store.object_exists(artifact_name)
        cached = artifact_cache[artifact_name]

        if cached:
            source: Literal["artifact", "triggered rebuild"] = "artifact"
        else:
            # Orphan-pin check: artifact isn't in the upstream store. Either upstream
            # CI is racing this resolve (its build is mid-upload), the pin is dead,
            # or the producer hasn't been rebuilt since one of its own deps moved
            # (timing skew). Tell these apart by looking at workflow run state, then
            # by classifying any non-running pin.
            run_key = (spec.repo, sha)
            if run_key not in run_state_cache:
                run_state_cache[run_key] = has_in_flight_run(spec.repo, sha, token)
            if run_state_cache[run_key]:
                # Producer CI is already building this; we wait, no rebuild
                # triggered. fetch_deps polls and downloads from the store.
                source = "artifact"
            else:
                # Match the producer against what THIS dep actually requests, not
                # the raw consumer leg: build-type / platform / options can differ
                # per-dep (e.g. a moments consumer of a plain-Release producer).
                # Producer legs name discriminators canonically, so override the
                # canonical keys; compiler fields keep their per-field consumer
                # values (the producer discriminates on its own compiler names).
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

    # Visit each direct dep of the local manifest.
    for spec in own_deps:
        visit(spec, matrix_entry)

    deps_resolved = [visited[name] for name in order]

    # Compute own artifact name using direct deps' artifact names for the hash.
    direct_dep_artifact_names = [visited[s.package].artifact_name for s in own_deps]
    own_compiler = _join_compilers(own.compiler_inputs, matrix_entry, context=f"[package] '{own.name}'")
    own_build_type = str(matrix_entry.get("build-type", "Release"))
    own_platform = compute_platform_slug(str(matrix_entry.get("platform", "")))
    # Whether the OWN artifact is python-version-axed is determined by the
    # leg itself, not by the [package].needs-python flag. The flag remains
    # the right source of truth for [[deps]] entries (where pip-install
    # behaviour follows the dep's declared needs-python), but for the OWN
    # package's identity any leg that declares python-version is by
    # definition producing a per-Python-version artifact.
    #
    # The bug this guards against: ecflow's [package] has needs-python =
    # false (because the primary [matrix.build] doesn't care about Python),
    # but its secondary [matrix.build-python] legs DO have python-version.
    # The earlier `if own.needs_python else None` gate dropped the suffix,
    # which made all 3 py-version legs of the same compiler collide on the
    # artifact name and one upload-artifact "win" while the others 404'd.
    own_python_raw = str(matrix_entry.get("python-version", "")).strip()
    own_python: str | None = own_python_raw or None

    # The OWN build's option config comes straight from this leg (it identifies what
    # we publish, e.g. cxxmath built with stochastic-moments).
    own_option = _as_option(matrix_entry.get("options"), context=f"[package] '{own.name}'")

    own_deps_hash = compute_deps_hash8(direct_dep_artifact_names)
    own_artifact = make_artifact_name(
        # Per-kind override (used when a kind publishes a secondary artifact in
        # the same repo) falls back to the package's primary prefix.
        prefix=PackageName(own_prefix_override) if own_prefix_override else own.prefix,
        sha=own_sha,
        deps_hash8=own_deps_hash,
        platform_slug=own_platform,
        compiler=own_compiler,
        build_type=own_build_type,
        python_version=own_python,
        option=own_option,
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
    """
    Walk the dep graph layer-by-layer using GraphQL aliased queries.

    Returns:
      manifest_cache: (repo, ref) -> Manifest
      sync_exists_by_repo: repo -> bool (does sync_branch exist there?)
    """
    manifest_cache: dict[tuple[Repo, Ref], Manifest] = {}
    sync_exists: dict[Repo, bool] = {}
    queue: list[tuple[Repo, Ref]] = [(d.repo, d.ref) for d in root_deps]

    for _depth in range(max_depth):
        # Filter to (repo, ref) we haven't seen yet.
        layer = [(r, ref) for (r, ref) in queue if (r, ref) not in manifest_cache]
        if not layer:
            break

        results = fetch_manifests_layer(layer, sync_branch, token, manifest_path)

        next_queue: list[tuple[Repo, Ref]] = []
        for (repo, ref), (text, sync_present) in results.items():
            if sync_branch:
                sync_exists.setdefault(repo, sync_present)
                # If sync-branch exists upstream, the EFFECTIVE ref to fetch is the sync branch,
                # not the pinned ref. Re-queue with the sync-branch ref so the actual manifest
                # we use for resolution comes from there.
                if sync_present and ref != sync_branch:
                    next_queue.append((repo, sync_branch))
                    continue
            if text is None:
                # No manifest at this ref. Treat as a leaf with no further deps.
                # We still need a Manifest object so resolve_leg knows there are no sub-deps.
                manifest_cache[(repo, ref)] = Manifest(
                    package=PackageSpec(
                        name=repo.split("/", 1)[1],
                        prefix=PackageName(""),
                        repo=repo,
                        compiler_inputs=(),
                        needs_python=False,
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


def write_outputs(outputs: Mapping[str, str]) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            for key, value in outputs.items():
                # Multi-line values need heredoc-style delimited output.
                if "\n" in value:
                    delim = f"EOF_{hashlib.sha1(value.encode()).hexdigest()[:8]}"
                    f.write(f"{key}<<{delim}\n{value}\n{delim}\n")
                else:
                    f.write(f"{key}={value}\n")
    else:
        for key, value in outputs.items():
            print(f"{key}={value}")


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

    # Decide which sync-branch (if any) to look for upstream.
    sync_branch: Ref | None = None
    if current_branch and _SYNC_BRANCH_RE.match(current_branch):
        sync_branch = Ref(current_branch)

    token = select_token()

    own_sha = _resolve_own_sha(str(local_manifest.package.repo), current_branch, token)

    # 1. BFS-load all transitive upstream manifests (collapsed into ≤depth GraphQL calls).
    manifest_cache, sync_exists = bfs_load_manifests(
        root_deps=local_manifest.deps,
        sync_branch=sync_branch,
        token=token,
        manifest_path=upstream_manifest_path,
    )

    # 2. For each requested matrix block, resolve every leg.
    sha_cache: dict[tuple[Repo, Ref], Sha] = {}
    artifact_cache: dict[ArtifactName, bool] = {}
    run_state_cache: dict[tuple[Repo, Sha], bool] = {}
    matrices_out: dict[str, dict[str, Any]] = {}

    # Consumer-driven dispatch is only attempted when the resolve-deps action
    # minted an App token and passed it via DISPATCH_TOKEN. Without it, orphan
    # pins still hard-fail the way they did pre-recovery.
    dispatch_token = os.environ.get("DISPATCH_TOKEN", "").strip()
    can_dispatch = bool(dispatch_token)
    dispatch_plans: dict[tuple[Repo, Ref], DispatchPlan] = {}

    matrix_names = [m.strip() for m in matrix.split(",") if m.strip()]

    for mname in matrix_names:
        include = local_manifest.matrix.get(mname, [])
        if not include:
            print(f"::warning::No [matrix.{mname}.include] in manifest; skipping.", file=sys.stderr)
            continue

        out_include: list[dict[str, Any]] = []
        own_prefix_override = local_manifest.artifact_prefix_by_kind.get(mname)
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
                dispatch_plans=dispatch_plans,
                own_prefix_override=own_prefix_override,
            )
            cmake_paths = [str(d.install_path) for d in deps_resolved]
            all_artifact_names = [d.artifact_name for d in deps_resolved]
            resolved_block = {
                "cmake-prefix-path": ";".join(cmake_paths),
                "all-artifact-names": " ".join(all_artifact_names),
                "all-artifact-sources": " ".join(d.source for d in deps_resolved),
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
                "direct-artifact-names": " ".join(
                    next(d.artifact_name for d in deps_resolved if d.name == s.package) for s in local_manifest.deps
                ),
            }
            merged = {**entry, "_resolved": resolved_block}
            out_include.append(merged)

        matrices_out[mname] = {"include": out_include}

    # 3. Dispatch every timing-skew producer once, deduped on (repo, ref). The
    # dispatch happens AFTER all legs resolve so a single producer flagged across
    # multiple matrix legs only gets dispatched once. Wait for each new run to
    # appear in the producer's API before exiting; this prevents a downstream
    # race where fetch_deps would see "no runs" and bail.
    if dispatch_plans:
        dispatcher_repo = os.environ.get("GITHUB_REPOSITORY", "")
        branch = current_branch or os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or "main"
        rebuild_repos = ", ".join(sorted(p.repo for p in dispatch_plans.values()))
        print(
            f"::notice::{len(dispatch_plans)} upstream package(s) need a rebuild before this repo can "
            f"build: {rebuild_repos}. Dispatching their CI now; this run's build jobs will wait for them."
        )
        for plan in dispatch_plans.values():
            # fallback-ref is what pick-ref uses inside the dispatched workflow
            # when the dispatcher's branch doesn't exist in the producer. The
            # right answer is the producer's pinned ref — the one the resolver
            # was looking for in the first place — not a hardcoded "main" that
            # may not exist (e.g. ecbuild is pinned to "fix_interface_exports"
            # and has no "main").
            dispatch_producer_workflow(
                plan=plan,
                dispatcher_repo=dispatcher_repo,
                dispatcher_sha=str(own_sha),
                branch=branch,
                fallback_ref=str(plan.ref),
                token=dispatch_token,
            )

    # Emit one matrix-<name>=<json> output per requested block, plus a json= blob
    # holding all of them keyed by name (for diagnostics / future use).
    outputs: dict[str, str] = {
        f"matrix-{mname}": json.dumps(mdata, separators=(",", ":")) for mname, mdata in matrices_out.items()
    }
    outputs["json"] = json.dumps(matrices_out, separators=(",", ":"))
    write_outputs(outputs)

    # Echo a brief summary to stdout for human debugging.
    print(
        f"Resolved {sum(len(v['include']) for v in matrices_out.values())} matrix legs across "
        f"{len(matrices_out)} matrix block(s)."
    )
    # Per-matrix-leg dump so the resolve job log shows what each downstream job will fetch.
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
