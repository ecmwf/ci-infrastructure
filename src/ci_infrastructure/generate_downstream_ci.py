#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
generate_downstream_ci.py

Reads every consumer repo's .ci/manifest.toml, validates the cross-repo
trigger / needs graph, and emits two generated files per repo:

  - .github/workflows/cross-repo-trigger.yml — has two entry points sharing
    typed inputs (from-repo, from-sha, from-jobs, rebuild-request, branch,
    fallback-ref): `workflow_call`, used by the upstream-driven orchestrator
    (trigger-downstream.yml) which sets `from-jobs=["pkg/kind", ...]` (every
    originator kind reaching this consumer); and `workflow_dispatch` (plus a
    dispatch-only `dispatch-id`), used by the consumer-driven recovery path
    (resolve_deps from inside a consumer's run when a producer's artifact is
    missing), which sets `rebuild-request=true`. Per-kind jobs gate on
    `contains(fromJSON(inputs.from-jobs), 'pkg/kind')` / `rebuild-request` via
    the kind's `triggers` field. Artifacts publish to shared S3 keyed by a deterministic
    name (independent of the producing run), so it no longer matters whether
    this runs standalone or nested inside the upstream's run. Runtime
    branch-matching happens in the resolve job's pick-ref step.

  - .github/workflows/trigger-downstream.yml (only for repos with consumers)
    — an orchestrator triggered by `workflow_call` from the upstream's ci.yml
    once CI has passed. It flat-fans-out one job per consumer package; each job
    invokes the consumer's cross-repo-trigger.yml as a reusable workflow
    (workflow_call) with `secrets: inherit`, so GitHub waits for it natively
    and surfaces its jobs on the upstream PR — no dispatch, no API polling. The
    ref is taken from the upstream's [[trigger-downstream]].ref entries
    (transitive paths must agree on the ref or generation fails). A consumer
    reached by several originator kinds still gets ONE caller job; its
    `from-jobs` carries the whole set so a single call wakes all its chains.

    EXCEPTION (log isolation): when a PUBLIC upstream fans out to a PRIVATE
    consumer, a reusable-workflow call would run the private consumer's jobs
    inside the public run and expose its logs. Such an edge is instead emitted
    as a workflow_dispatch (via the dispatch-and-wait action), so the consumer's
    run — and logs — stay in the private repo. The orchestrator job waits on
    that run's conclusion (polling run status, never logs) so it still gates the
    upstream PR; the consumer's per-kind jobs additionally post per-leg check
    runs back for at-a-glance detail. Every other edge (public->public,
    private->private, private->public) keeps the native reusable-workflow call.
    Visibility is declared per repo via [package].visibility.

Schema additions consumed (sibling to the existing [[deps]] / [[matrix.X.include]]):

    [package]
    visibility = "public"        # optional; "private" (default) | "public".
    # ^ Default "private" (fail closed): unlabelled repos are dispatched (not
    #   called via `uses:`) by public upstreams so their CI logs never render
    #   into a public run. Mark a repo "public" to opt into the native
    #   reusable-workflow path.

    [[trigger-downstream]]
    repo = "owner/consumer-repo"
    ref  = "main"                # required: ref to pin orchestrator `uses:`

    [matrix.build]
    triggers = ["upstream-change", "rebuild-request"]
    # ^ "upstream-change" — fire when an upstream's trigger-downstream
    #    orchestrator dispatches us
    # ^ "rebuild-request" — fire when a consumer dispatches us because our
    #    artifact was missing
    # An empty/missing triggers means push/PR-only (no cross-repo entry).
    needs = ["fortmath/build"]   # cross-repo: "<package-name>/<kind>"

    [matrix.test]
    triggers = ["upstream-change"]   # tests run on upstream change but NOT
                                     # on consumer rebuild (no artifact to
                                     # verify, wasted compute)
    reuse-matrix = "build"       # share the include legs of another kind
    needs = ["build"]            # local: bare kind name

    # ci.yml-only kinds (no triggers) need no [matrix.<kind>] table at all —
    # just [[matrix.<kind>.include]] legs. triggers defaults to empty;
    # needs/reuse-matrix on a non-triggered kind are never consulted.
    [[matrix.clang-tidy.include]]
    cxx-compiler = "clang++-18"
    ...

Invariants enforced (fail-loud, exit 1):

  - [[trigger-downstream]] graph is acyclic.
  - Every [[trigger-downstream]] target manifest declares a [[deps]] back at us
    (triggers ⊆ reverse-deps).
  - Local `needs` resolve to a real [matrix.<kind>] in the same manifest.
  - Cross-repo `needs = "P/K"` resolve to a [matrix.K] in the manifest whose
    [package].name == "P", whose triggers include "upstream-change", AND that
    upstream repo lists us in its [[trigger-downstream]] (otherwise the
    orchestrator never calls us).
  - The cross-repo job graph (only cross-repo edges) is acyclic.
  - `reuse-matrix` references a real kind in the same manifest.
  - Transitive trigger paths agree on the consumer ref (no ref disagreement
    across diamond closures — `gh workflow run --ref` can't be pinned to two
    values for the same consumer).
  - Per-orchestrator GHA cap: ≤220 estimated jobs (safety margin below the
    256-job-per-run hard cap).

Usage:

    cd <consumer-repo>
    python3 path/to/generate_downstream_ci.py [--check]

The script always operates on the cwd's manifest and fetches every sibling
manifest named in [[deps]] / [[trigger-downstream]] over the GitHub GraphQL
API (no clones, ≤2-3 batched queries per BFS layer). Auth: GH_TOKEN env, or
the gh CLI's keychain auth if no env token is set.

Without --check, regenerated YAML is written in place under
.github/workflows/. With --check, the step exits 1 and points the developer
at the regen command if any file is out of date.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import click
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ._errors import CIError
from ._github_api import fetch_manifests_layer, fetch_repo_head, select_token

GENERATED_HEADER: Final = (
    "# GENERATED FILE - DO NOT EDIT.\n"
    "# Regenerate via the ci-infrastructure-generate CLI.\n"
    "# Source of truth: each repo's .ci/manifest.toml.\n"
)

# Runner for the jobs that only talk to APIs and shuffle YAML — resolve,
# validate, and the dispatch-and-wait jobs that idle for a whole downstream run.
# ubuntu-slim is a 1-CPU/5GB GitHub-hosted runner billed at a lower rate, with
# git, gh, jq, tar and Python preinstalled, which is everything these need. It
# runs unprivileged, so nothing that wants Docker or a real build may use it.
SLIM_RUNNER: Final = "ubuntu-slim"


# Workflow YAML is built up as plain Python dicts and dumped via PyYAML;
# multi-line shell bodies are tagged with the _BlockScalar marker so they
# come out as literal block scalars (`|`) instead of quoted strings.

Step: TypeAlias = dict[str, Any]  # one entry of `steps:` inside a job


class _BlockScalar(str):
    """str subclass the custom YAML representer renders as a `|` block scalar.

    Use for multi-line `run:` bodies where preserving literal newlines matters
    and quoting would be unreadable.
    """


class _WorkflowDumper(yaml.SafeDumper):
    """SafeDumper that emits GA-friendly YAML 1.2 booleans.

    PyYAML defaults to YAML 1.1, where the literal `on`, `off`, `yes`, `no`
    (and capitalisation variants) are interpreted as booleans — so a dict
    key `"on"` round-trips as `'on':` with disambiguating quotes. GitHub
    Actions parses YAML 1.2, where only `true`/`false` are booleans, and
    workflow files canonically use bare `on:`. Replace the YAML 1.1 bool
    resolver with the YAML 1.2 one to drop the spurious quotes.
    """


# Reset the implicit resolvers, then re-add everything except the YAML 1.1
# bool resolver, plus a YAML 1.2 bool resolver that only matches true/false.
_WorkflowDumper.yaml_implicit_resolvers = {
    k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeDumper.yaml_implicit_resolvers.items()
}
_WorkflowDumper.add_implicit_resolver(  # type: ignore[no-untyped-call]
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _block_scalar_representer(dumper: _WorkflowDumper, data: _BlockScalar) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


_WorkflowDumper.add_representer(_BlockScalar, _block_scalar_representer)


def _dump_workflow(doc: Mapping[str, Any]) -> str:
    """Serialise a workflow dict to YAML using our preferred style.

    `sort_keys=False` preserves insertion order so `name`/`on`/`jobs` come out
    top-down. `width` is set high so long ${{ }} expressions don't get folded
    across lines.
    """
    out: str = yaml.dump(
        dict(doc),
        Dumper=_WorkflowDumper,
        sort_keys=False,
        default_flow_style=False,
        width=10**9,
        allow_unicode=True,
    )
    return out


# Valid values for [matrix.<kind>].triggers. Either or both may be set; the
# kind's generated `if:` filter becomes the OR of clauses for each opted-in
# trigger. An empty `triggers` (the default) means the kind is push/PR only
# and does NOT appear in cross-repo-trigger.yml at all.
TRIGGER_UPSTREAM_CHANGE: Final = "upstream-change"  # upstream's trigger-downstream orchestrator dispatched us
TRIGGER_REBUILD_REQUEST: Final = "rebuild-request"  # a consumer dispatched us to rebuild our artifact
_VALID_TRIGGERS: Final = frozenset({TRIGGER_UPSTREAM_CHANGE, TRIGGER_REBUILD_REQUEST})

# How a kind's build runs. "runner" is the default GitHub-runner path; "hpc"
# submits the kind's job_script as a SLURM job via the build-on-hpc action.
Execution: TypeAlias = Literal["runner", "hpc"]
EXECUTION_RUNNER: Final[Execution] = "runner"
EXECUTION_HPC: Final[Execution] = "hpc"

# A repo's [package].visibility. "private" repos must never have their CI logs
# rendered into another repo's run: a public upstream that fans out to a private
# consumer dispatches it (its run stays in the private repo) instead of calling
# it as a reusable workflow. See render_orchestrator_workflow.
Visibility: TypeAlias = Literal["public", "private"]
VISIBILITY_PUBLIC: Final[Visibility] = "public"
VISIBILITY_PRIVATE: Final[Visibility] = "private"


def _lane_suffix(lane: Execution) -> str:
    """Filename suffix distinguishing the two lanes' generated workflow files.

    The runner lane keeps the historical unsuffixed names (cross-repo-trigger.yml,
    trigger-downstream.yml); the hpc lane gets a `-hpc` sibling. Splitting the flow
    into two files per side (rather than one file gated by a `lane` input) keeps the
    concurrency groups distinct and avoids skipped jobs on the unused lane.
    """
    return "" if lane == EXECUTION_RUNNER else "-hpc"


def _status_context(lane: Execution) -> str:
    """Commit-status context the orchestrator posts back to the tested SHA.

    Exposed per lane (`downstream/runner`, `downstream/hpc`) so each can be made a
    required status check independently and block a merge on its own."""
    return f"downstream/{lane}"


class SchemaError(Exception):
    """A manifest violates the schema or a cross-repo invariant."""


@dataclass(frozen=True)
class MatrixKind:
    """One [matrix.<kind>] table plus its [[matrix.<kind>.include]] legs."""

    name: str
    triggers: frozenset[str]  # subset of _VALID_TRIGGERS; empty -> push/PR only
    needs: Sequence[str]
    reuse_matrix: str | None
    legs: Sequence[dict[str, Any]]  # raw include entries (after reuse resolution)
    # How this kind's build runs. "runner" (default) invokes the manifest's
    # `action` composite on the GitHub runner. "hpc" instead submits the repo's
    # `job_script` as a SLURM job (via the shared build-on-hpc action) on a
    # login-node self-hosted runner. Everything else — resolve, fetch, artifact
    # identity, needs-graph — is identical between the two.
    execution: Execution
    # `action` is the composite the generated cross-repo-trigger.yml's per-kind
    # job invokes (e.g. './.github/actions/build-cxxmath'). Required when
    # `triggers` is non-empty and execution is "runner"; empty for HPC kinds
    # (which call the shared build-on-hpc action) and for push/PR-only kinds.
    action: str
    # For execution == "hpc": the repo-owned build recipe submitted as a SLURM
    # job (its #SBATCH header + module loads + cmake/ctest body). None otherwise.
    job_script: str | None
    # Matrix-leg fields the generator extracts into per-step outputs and threads
    # into the action's `with:` block. Each entry must be a key that appears in
    # at least one matrix leg (typo guard).
    forwarded_inputs: tuple[str, ...]
    # Outputs of the fetch-and-publish (download-only) step to forward into the
    # action's `with:` block. Today the only meaningful value is
    # "cmake-prefix-path"; leaf producers (ecbuild, stack-deps) leave this empty.
    forwarded_deps_outputs: tuple[str, ...]
    # Whether this kind produces an artifact to upload. Build kinds typically
    # publish (default); test kinds consume an already-uploaded artifact and
    # publish nothing. When false the render emits no publish step and instead
    # threads `own-artifact-name` into the action's `with:` block so it can
    # download the artifact it's testing.
    publishes: bool
    # Optional override for the artifact-name prefix this kind publishes.
    # Defaults to [package].prefix (handled in the resolver). Set when a kind
    # in the same repo publishes a SECONDARY artifact under a different name
    # — e.g. ecflow's `build-python` publishes `ecflowmath-python-*` while
    # `build` publishes `ecflowmath-*`. Without this override, both kinds
    # would publish under the package's primary prefix and collide.
    artifact_prefix: str | None


@dataclass(frozen=True)
class TriggerDownstream:
    repo: str
    ref: str  # consumer ref the orchestrator pins `uses: …@<ref>` to.


@dataclass(frozen=True)
class DepRef:
    """Just the bits of [[deps]] needed for validation (not full resolution)."""

    repo: str
    package: str


@dataclass
class Manifest:
    """Parsed view of one repo's .ci/manifest.toml."""

    path: Path  # filesystem path to the manifest
    repo_root: Path  # the repo directory (manifest.path.parents[1])
    package_name: str
    repo: str  # owner/repo
    visibility: Visibility = VISIBILITY_PRIVATE
    deps: list[DepRef] = field(default_factory=list)
    triggers: list[TriggerDownstream] = field(default_factory=list)
    matrices: dict[str, MatrixKind] = field(default_factory=dict)


_RESERVED_MATRIX_KEYS: Final = frozenset(
    {
        "include",
        "triggers",
        "needs",
        "reuse-matrix",
        "execution",
        "action",
        "job-script",
        "forwarded-inputs",
        "forwarded-deps-outputs",
        "publishes",
        "artifact-prefix",
    }
)
# Valid entries for `forwarded-deps-outputs` — must correspond to real outputs
# of `actions/fetch-and-publish` in `mode: download-only`.
_VALID_DEPS_OUTPUTS: Final = frozenset({"cmake-prefix-path"})
_ACTION_PATH_RE: Final = re.compile(r"^\./\.github/actions/[A-Za-z0-9_-]+$")
# Constrained to the same character class artifact-name parts use; rejects
# slashes, dots, spaces, and other shell-metacharacters that would corrupt
# downstream `gh api` / cache-key lookups.
_ARTIFACT_PREFIX_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


class _PackageRaw(BaseModel):
    # extra="ignore" — resolve_deps owns the full [package] schema (compiler-inputs,
    # needs-python, etc.); the generator only reads name + repo + visibility.
    # Re-validating the rest here would couple the two parsers.
    model_config = ConfigDict(extra="ignore")
    name: str
    repo: str
    # Absent -> "private" (fail closed: an unlabelled repo is treated as private
    # so a public upstream never exposes its logs; mark "public" to opt in to
    # the native reusable-workflow path). The Literal rejects any other value.
    visibility: Visibility = VISIBILITY_PRIVATE


class _DepRefRaw(BaseModel):
    model_config = ConfigDict(extra="ignore")
    repo: str
    package: str


class _TriggerDownstreamRaw(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    ref: str

    @model_validator(mode="before")
    @classmethod
    def _exact_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError(f"must be a table (got {type(data).__name__})")
        if set(data.keys()) != {"repo", "ref"}:
            raise ValueError(f"must define exactly 'repo' and 'ref' (got {sorted(data.keys())})")
        return data

    @field_validator("ref")
    @classmethod
    def _ref_nonempty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("'ref' must be a non-empty string")
        return s


class _MatrixKindRaw(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    triggers: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    reuse_matrix: str | None = Field(default=None, alias="reuse-matrix")
    include: tuple[dict[str, Any], ...] = ()
    execution: Execution = EXECUTION_RUNNER
    action: str = ""
    job_script: str = Field(default="", alias="job-script")
    forwarded_inputs: tuple[str, ...] = Field(default=(), alias="forwarded-inputs")
    forwarded_deps_outputs: tuple[str, ...] = Field(default=(), alias="forwarded-deps-outputs")
    publishes: bool = True
    artifact_prefix: str | None = Field(default=None, alias="artifact-prefix")

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError(f"must be a table (got {type(data).__name__})")
        unknown = set(data.keys()) - _RESERVED_MATRIX_KEYS
        if unknown:
            raise ValueError(f"has unknown key(s) {sorted(unknown)}; allowed: {sorted(_RESERVED_MATRIX_KEYS)}")
        return data

    @field_validator("triggers")
    @classmethod
    def _triggers_valid(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [t for t in v if t not in _VALID_TRIGGERS]
        if bad:
            raise ValueError(f"triggers entries must be drawn from {sorted(_VALID_TRIGGERS)}; got unknown: {bad}")
        # Reject duplicates so the schema reads as a set.
        if len(set(v)) != len(v):
            raise ValueError(f"triggers must not contain duplicates: {list(v)}")
        return v

    @field_validator("action")
    @classmethod
    def _action_path_shape(cls, v: str) -> str:
        # Empty is fine — kinds without triggers don't need an action. The
        # required-when-triggered check happens after include legs are resolved
        # so we can emit a precise message.
        if v and not _ACTION_PATH_RE.fullmatch(v):
            raise ValueError(f"action must be a local composite path like './.github/actions/<name>'; got {v!r}")
        return v

    @field_validator("forwarded_inputs")
    @classmethod
    def _forwarded_inputs_shape(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError(f"forwarded-inputs must not contain duplicates: {list(v)}")
        bad = [x for x in v if not x or x != x.strip()]
        if bad:
            raise ValueError(f"forwarded-inputs entries must be non-empty trimmed strings: {bad}")
        return v

    @field_validator("forwarded_deps_outputs")
    @classmethod
    def _forwarded_deps_outputs_valid(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [x for x in v if x not in _VALID_DEPS_OUTPUTS]
        if bad:
            raise ValueError(
                f"forwarded-deps-outputs entries must be drawn from {sorted(_VALID_DEPS_OUTPUTS)}; got unknown: {bad}"
            )
        if len(set(v)) != len(v):
            raise ValueError(f"forwarded-deps-outputs must not contain duplicates: {list(v)}")
        return v

    @field_validator("artifact_prefix")
    @classmethod
    def _artifact_prefix_shape(cls, v: str | None) -> str | None:
        # None (default) means "use [package].prefix" — handled in the resolver.
        # An empty/whitespace string is almost always a typo; reject loudly so
        # the artifact-name suffix doesn't start with a stray '-'.
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            raise ValueError("artifact-prefix must be a non-empty string (or omitted to inherit [package].prefix)")
        if not _ARTIFACT_PREFIX_RE.fullmatch(stripped):
            raise ValueError(
                f"artifact-prefix must match {_ARTIFACT_PREFIX_RE.pattern!r} "
                f"(letters, digits, hyphen, underscore); got {v!r}"
            )
        return stripped


class _ManifestRaw(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    package: _PackageRaw
    deps: tuple[_DepRefRaw, ...] = ()
    trigger_downstream: tuple[_TriggerDownstreamRaw, ...] = Field(default=(), alias="trigger-downstream")
    matrix: dict[str, _MatrixKindRaw] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_duplicate_triggers(self) -> _ManifestRaw:
        seen: set[str] = set()
        for t in self.trigger_downstream:
            if t.repo in seen:
                raise ValueError(f"duplicate [[trigger-downstream]] for {t.repo!r}")
            seen.add(t.repo)
        return self


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    """Translate the first pydantic error into a single line that mirrors the
    pre-pydantic SchemaError messages.

    The translator turns the pydantic loc tuple into TOML-ish notation
    (`[matrix.build]`, `[[trigger-downstream]][1]`) so the existing
    `pytest.raises(SchemaError, match=...)` assertions keep working.
    """
    err = exc.errors()[0]
    loc: tuple[str | int, ...] = tuple(err["loc"])
    msg = err["msg"]
    # Strip the "Value error, " prefix that pydantic adds to ValueError raised
    # from @model_validator / @field_validator hooks; our hooks already produce
    # the user-facing wording.
    msg = msg.removeprefix("Value error, ")
    prefix = _format_loc(loc)
    sep = " " if prefix and not prefix.endswith(" ") else ""
    return f"{path}: {prefix}{sep}{msg}".rstrip()


def _format_loc(loc: tuple[str | int, ...]) -> str:
    """Convert a pydantic loc tuple into a TOML-ish prefix.

    Examples:
      ()                                -> ""
      ("package",)                      -> "[package]"
      ("package", "name")               -> "[package].name"
      ("matrix", "build")               -> "[matrix.build]"
      ("matrix", "build", "needs")      -> "[matrix.build].needs"
      ("trigger-downstream", 0)         -> "[[trigger-downstream]][0]"
      ("trigger-downstream", 1, "ref")  -> "[[trigger-downstream]][1].ref"
      ("deps", 2, "package")            -> "[[deps]][2].package"
    """
    if not loc:
        return ""
    head = loc[0]
    if head in {"trigger-downstream", "trigger_downstream"}:
        rest = loc[1:]
        idx = rest[0] if rest and isinstance(rest[0], int) else None
        prefix = "[[trigger-downstream]]"
        if idx is not None:
            prefix += f"[{idx}]"
            tail = rest[1:]
        else:
            tail = rest
        if tail:
            prefix += "." + ".".join(str(p) for p in tail)
        return prefix
    if head == "deps":
        rest = loc[1:]
        idx = rest[0] if rest and isinstance(rest[0], int) else None
        prefix = "[[deps]]"
        if idx is not None:
            prefix += f"[{idx}]"
            tail = rest[1:]
        else:
            tail = rest
        if tail:
            prefix += "." + ".".join(str(p) for p in tail)
        return prefix
    if head == "matrix":
        rest = loc[1:]
        if rest:
            kind = rest[0]
            prefix = f"[matrix.{kind}]"
            tail = rest[1:]
            if tail:
                prefix += "." + ".".join(str(p) for p in tail)
            return prefix
        return "[matrix]"
    if head == "package":
        rest = loc[1:]
        prefix = "[package]"
        if rest:
            prefix += "." + ".".join(str(p) for p in rest)
        return prefix
    return ".".join(str(p) for p in loc)


def parse_manifest(path: Path) -> Manifest:
    """Load one .ci/manifest.toml and apply only the validation we need here.

    Intentionally narrow: we don't re-validate every existing field; the existing
    resolve_deps.py owns that. We only parse what the generator needs.
    """
    with path.open("rb") as fh:
        raw_dict = tomllib.load(fh)
    return _build_manifest(path, raw_dict)


def parse_manifest_text(text: str, path: Path) -> Manifest:
    """Parse a manifest TOML *string* — used in --fetch mode where sibling manifests
    arrive as GraphQL blobs and have no real on-disk path. The caller passes a
    synthetic `path` (e.g. `Path("github://owner/repo@HEAD/.ci/manifest.toml")`)
    used only for error messages and as `repo_root` for symmetry with on-disk
    manifests; we never write to it.
    """
    return _build_manifest(path, tomllib.loads(text))


def _build_manifest(path: Path, raw_dict: dict[str, Any]) -> Manifest:
    try:
        raw = _ManifestRaw.model_validate(raw_dict)
    except ValidationError as exc:
        raise SchemaError(_format_validation_error(path, exc)) from exc

    matrices = _resolve_matrices(path, raw.matrix)

    return Manifest(
        path=path,
        repo_root=path.parents[1],
        package_name=raw.package.name,
        repo=raw.package.repo,
        visibility=raw.package.visibility,
        deps=[DepRef(repo=d.repo, package=d.package) for d in raw.deps],
        triggers=[TriggerDownstream(repo=t.repo, ref=t.ref) for t in raw.trigger_downstream],
        matrices=matrices,
    )


def _resolve_matrices(path: Path, raw_matrix: Mapping[str, _MatrixKindRaw]) -> dict[str, MatrixKind]:
    """Second pass: resolve reuse-matrix into concrete legs and project to MatrixKind.

    Pydantic has already enforced the per-kind shape (no unknown keys, correct
    types). What's left is the cross-kind reference: `reuse-matrix = "X"` must
    point at a kind that exists, isn't itself a reuse-matrix, and isn't combined
    with an explicit `include`.
    """
    resolved: dict[str, MatrixKind] = {}
    for kind, body in raw_matrix.items():
        if body.reuse_matrix is not None and body.include:
            raise SchemaError(
                f"{path}: [matrix.{kind}] sets both 'reuse-matrix' and [[matrix.{kind}.include]]; pick one"
            )

        if body.reuse_matrix is not None:
            target = raw_matrix.get(body.reuse_matrix)
            if target is None:
                raise SchemaError(
                    f"{path}: [matrix.{kind}].reuse-matrix = {body.reuse_matrix!r} "
                    f"but [matrix.{body.reuse_matrix}] does not exist"
                )
            if target.reuse_matrix is not None:
                raise SchemaError(
                    f"{path}: [matrix.{kind}].reuse-matrix = {body.reuse_matrix!r} is itself a reuse-matrix; "
                    f"chained reuse is not supported"
                )
            legs = tuple(target.include)
        else:
            legs = tuple(body.include)

        # `execution` picks the build path and dictates which of action /
        # job-script is required. HPC kinds call the shared build-on-hpc action,
        # so they carry a job-script (the repo's recipe) and must NOT set action;
        # runner kinds are the mirror image.
        if body.execution == EXECUTION_HPC:
            if body.action:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] sets execution = 'hpc' and `action`; "
                    "HPC kinds run the shared build-on-hpc action — drop `action` and set `job-script`"
                )
            if body.triggers and not body.job_script:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] declares triggers = {sorted(body.triggers)!r} with "
                    "execution = 'hpc' but has no `job-script`; there is no build recipe to submit"
                )
        else:
            if body.job_script:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] sets `job-script` but execution is 'runner'; "
                    "`job-script` only applies to execution = 'hpc'"
                )
            # When a runner kind opts into any trigger, the generator's per-kind
            # job needs something to call — the manifest must name a composite.
            if body.triggers and not body.action:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] declares triggers = {sorted(body.triggers)!r} "
                    "but has no `action`; cross-repo-trigger.yml has nothing to invoke"
                )

        # Every forwarded-input must appear as a key in at least one matrix leg,
        # otherwise it's a typo that would only surface as an empty `require`
        # at workflow runtime.
        if body.forwarded_inputs:
            available = {k for leg in legs for k in leg}
            missing = [x for x in body.forwarded_inputs if x not in available]
            if missing:
                raise SchemaError(
                    f"{path}: [matrix.{kind}].forwarded-inputs references field(s) {missing} "
                    f"that no [[matrix.{kind}.include]] leg declares; "
                    f"available fields: {sorted(available)}"
                )

        resolved[kind] = MatrixKind(
            name=kind,
            triggers=frozenset(body.triggers),
            needs=tuple(body.needs),
            reuse_matrix=body.reuse_matrix,
            legs=legs,
            execution=body.execution,
            action=body.action,
            job_script=body.job_script or None,
            forwarded_inputs=tuple(body.forwarded_inputs),
            forwarded_deps_outputs=tuple(body.forwarded_deps_outputs),
            publishes=body.publishes,
            artifact_prefix=body.artifact_prefix,
        )
    return resolved


@dataclass(frozen=True)
class JobRef:
    """A reference like 'cxxmath/build' parsed into (package, kind)."""

    package: str
    kind: str


def _split_need(raw: str) -> tuple[str | None, str]:
    """Return (package, kind) for cross-repo refs, (None, kind) for local ones."""
    if "/" in raw:
        pkg, _, kind = raw.partition("/")
        return pkg, kind
    return None, raw


def validate_graph(manifests: Sequence[Manifest]) -> None:
    """Run every cross-repo invariant. Raises SchemaError on the first violation."""
    by_repo = {m.repo: m for m in manifests}
    by_pkg = {m.package_name: m for m in manifests}

    if len(by_repo) != len(manifests):
        # Two manifests for the same owner/repo would silently overwrite each other downstream.
        seen: dict[str, Path] = {}
        for m in manifests:
            if m.repo in seen:
                raise SchemaError(f"duplicate manifest for repo {m.repo}: {seen[m.repo]} and {m.path}")
            seen[m.repo] = m.path

    if len(by_pkg) != len(manifests):
        seen2: dict[str, Path] = {}
        for m in manifests:
            if m.package_name in seen2:
                raise SchemaError(f"duplicate package name {m.package_name!r}: {seen2[m.package_name]} and {m.path}")
            seen2[m.package_name] = m.path

    _check_subset_invariant(manifests, by_repo)
    _check_trigger_cycles(manifests, by_repo)
    _check_needs(manifests, by_pkg, by_repo)
    _check_reuse_matrix_targets(manifests)
    _check_leg_identity_uniqueness(manifests)


# Fields that schedule a build (which runner / image delivers the tools) but do
# NOT enter the artifact name. Two publishing legs that differ only in these
# would upload different builds under one artifact name — a self-collision a
# by-name lookup resolves non-deterministically.
_SCHEDULING_FIELDS: Final = frozenset({"runs-on", "container", "site"})


def _check_leg_identity_uniqueness(manifests: Sequence[Manifest]) -> None:
    """No two legs of the same publishing kind may share an artifact identity.

    The artifact name is built from `platform` + the compiler fields +
    `build-type` + `python-version` — every leg field EXCEPT the scheduling
    fields (`runs-on`, `container`). So two publishing legs that are identical
    once those are dropped resolve to the same artifact name; if they're built
    in different environments (e.g. a host runner vs a container, or two images)
    they publish different bytes under one name and a by-name fetch picks one
    non-deterministically. Non-publishing kinds (test) are exempt — they upload
    nothing. (This guards the bug where a host smoke leg shared
    platform=ubuntu-24.04 with a container leg.)
    """
    for m in manifests:
        for kind, mk in m.matrices.items():
            if not mk.publishes:
                continue
            seen: dict[frozenset[tuple[str, str]], dict[str, Any]] = {}
            for leg in mk.legs:
                identity = frozenset((k, str(v)) for k, v in leg.items() if k not in _SCHEDULING_FIELDS)
                if identity in seen:
                    shared = dict(sorted(identity))
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}] has two legs with the same artifact identity "
                        f"{shared} — they differ only in runs-on/container, which are not part of "
                        f"the artifact name, so they would publish different builds under one name. "
                        f"Give them distinct platform/compiler/build-type/python-version (e.g. a "
                        f"separate platform like 'gh-ubuntu-24.04' for the host leg), or drop one."
                    )
                seen[identity] = leg


def _check_subset_invariant(manifests: Sequence[Manifest], by_repo: Mapping[str, Manifest]) -> None:
    """Triggers are a subset of reverse-deps: if A triggers B, then B must list A as a dep."""
    for m in manifests:
        for t in m.triggers:
            target = by_repo.get(t.repo)
            if target is None:
                # Trigger to a repo we don't know about (external). Skip; we can't validate.
                continue
            depends_on_us = any(d.repo == m.repo for d in target.deps)
            if not depends_on_us:
                raise SchemaError(
                    f"{m.path}: [[trigger-downstream]] points at {t.repo}, but "
                    f"{target.path} does not list {m.repo} as a [[deps]] entry "
                    f"(triggers must be a subset of the reverse-deps graph)"
                )


def _check_trigger_cycles(manifests: Sequence[Manifest], by_repo: Mapping[str, Manifest]) -> None:
    """Plain DFS on the trigger graph."""
    graph: dict[str, list[str]] = {m.repo: [t.repo for t in m.triggers] for m in manifests}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(lambda: WHITE)

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        for nxt in graph.get(node, []):
            if nxt not in by_repo:
                continue  # external trigger, can't follow
            if color[nxt] == GRAY:
                cycle = stack[stack.index(nxt) :] + [nxt]
                raise SchemaError("trigger-downstream cycle: " + " -> ".join(cycle))
            if color[nxt] == WHITE:
                dfs(nxt, stack + [nxt])
        color[node] = BLACK

    for m in manifests:
        if color[m.repo] == WHITE:
            dfs(m.repo, [m.repo])


def _check_needs(
    manifests: Sequence[Manifest],
    by_pkg: Mapping[str, Manifest],
    by_repo: Mapping[str, Manifest],
) -> None:
    """Resolve every entry in matrix.needs and verify cross-repo fan-out reachability."""
    cross_edges: list[tuple[Manifest, str, JobRef]] = []  # (downstream-manifest, downstream-kind, upstream-jobref)

    for m in manifests:
        for kind, mk in m.matrices.items():
            for need in mk.needs:
                pkg, target_kind = _split_need(need)
                if pkg is None:
                    if target_kind not in m.matrices:
                        raise SchemaError(
                            f"{m.path}: [matrix.{kind}].needs references local kind "
                            f"{target_kind!r}, but no [matrix.{target_kind}] exists"
                        )
                    continue
                # Cross-repo reference.
                upstream = by_pkg.get(pkg)
                if upstream is None:
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}].needs references unknown package "
                        f"{pkg!r} (no manifest in scope has package.name = {pkg!r})"
                    )
                up_kind = upstream.matrices.get(target_kind)
                if up_kind is None:
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}].needs references {pkg}/{target_kind}, "
                        f"but {upstream.path} has no [matrix.{target_kind}]"
                    )
                if not up_kind.triggers:
                    # The upstream kind exists but is push/PR only (no triggers).
                    # Cross-repo needs implies the kind should participate in
                    # cross-repo wiring; otherwise the originator string in the
                    # orchestrator's dispatch can't reach anything meaningful.
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}].needs references {pkg}/{target_kind}, "
                        f"but [matrix.{target_kind}] in {upstream.path} has no triggers — "
                        f"the kind exists for push/PR runs only and can't originate a "
                        f"cross-repo dispatch"
                    )
                # Reachability: upstream must trigger us, or this is dead code.
                if not any(t.repo == m.repo for t in upstream.triggers):
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}] needs {pkg}/{target_kind} but "
                        f"{upstream.path} has no [[trigger-downstream]] back to {m.repo} - "
                        f"the orchestrator will never call us"
                    )
                cross_edges.append((m, kind, JobRef(pkg, target_kind)))

    _check_cross_repo_job_cycles(manifests, by_pkg, cross_edges)


def _check_cross_repo_job_cycles(
    manifests: Sequence[Manifest],
    by_pkg: Mapping[str, Manifest],
    cross_edges: Sequence[tuple[Manifest, str, JobRef]],
) -> None:
    # Node = (package, kind). Edges from cross-repo needs only.
    out_edges: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for downstream_m, downstream_kind, upstream_ref in cross_edges:
        out_edges[(downstream_m.package_name, downstream_kind)].append((upstream_ref.package, upstream_ref.kind))

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple[str, str], int] = defaultdict(lambda: WHITE)

    def dfs(node: tuple[str, str], stack: list[tuple[str, str]]) -> None:
        color[node] = GRAY
        for nxt in out_edges.get(node, []):
            if color[nxt] == GRAY:
                cycle = stack[stack.index(nxt) :] + [nxt]
                raise SchemaError("cross-repo needs cycle: " + " -> ".join(f"{p}/{k}" for p, k in cycle))
            if color[nxt] == WHITE:
                dfs(nxt, stack + [nxt])
        color[node] = BLACK

    for node in list(out_edges.keys()):
        if color[node] == WHITE:
            dfs(node, [node])


def _check_reuse_matrix_targets(manifests: Sequence[Manifest]) -> None:
    # Already validated structurally during parse, but check that any reuse target
    # has at least one include leg (otherwise the consumer will produce an empty matrix).
    for m in manifests:
        for kind, mk in m.matrices.items():
            if not mk.legs and (mk.triggers or mk.reuse_matrix is not None):
                raise SchemaError(
                    f"{m.path}: [matrix.{kind}] has no include legs (directly or via "
                    f"reuse-matrix={mk.reuse_matrix!r}); cannot generate a runnable matrix"
                )


def job_id(package: str, kind: str) -> str:
    """GitHub Actions job IDs may not contain '/'; we replace it with '__'."""
    return f"{_id_segment(package)}__{_id_segment(kind)}"


def _id_segment(s: str) -> str:
    return s.replace("/", "_").replace("-", "_")


def transitive_cross_repo_needs(m: Manifest, kind: str, by_pkg: Mapping[str, Manifest]) -> list[JobRef]:
    """Walk needs (local AND cross-repo) collecting every transitive cross-repo ref.

    The result is the upstream-job filter set: this `(package, kind)` job must
    run when the orchestrator calls us with ANY of these upstream-job inputs.
    Without recursing cross-repo, a consumer would only accept its direct
    upstream and a transitive call (e.g. orchestrator on fortran calling
    cxxmath-python with upstream-job=fortmath/build) would find no matching
    `if:` and skip silently.
    """
    cross: list[JobRef] = []
    seen_refs: set[tuple[str, str]] = set()
    seen_local: set[tuple[str, str]] = set()  # (package, kind) — avoid re-walking

    def walk(manifest: Manifest, k: str) -> None:
        key = (manifest.package_name, k)
        if key in seen_local:
            return
        seen_local.add(key)
        mk = manifest.matrices.get(k)
        if mk is None:
            return
        for need in mk.needs:
            pkg, target_kind = _split_need(need)
            if pkg is None:
                walk(manifest, target_kind)
                continue
            ref_key = (pkg, target_kind)
            if ref_key not in seen_refs:
                seen_refs.add(ref_key)
                cross.append(JobRef(pkg, target_kind))
            upstream = by_pkg.get(pkg)
            if upstream is not None:
                walk(upstream, target_kind)

    walk(m, kind)
    return cross


def render_workflow(m: Manifest, by_pkg: Mapping[str, Manifest], *, lane: Execution) -> str | None:
    """Emit a repo's .github/workflows/cross-repo-trigger{-hpc}.yml as a string,
    or None if no kind in the repo's matrix opts into cross-repo dispatch on `lane`.

    The emitted workflow is a workflow_dispatch entry point fired from two
    directions: the upstream's trigger-downstream.yml orchestrator (after
    a successful upstream CI, with `from-jobs=["pkg/kind", ...]`) and resolve_deps
    from inside a consumer's run (when a producer's artifact is stale,
    with `rebuild-request=true`). A kind appears in this workflow iff its
    `triggers` list is non-empty; the filter rendered is the OR of the
    clauses for each opted-in trigger. Leaf producers (no upstream-change
    refs, only rebuild-request) get a generated workflow too.
    """
    # One file per lane: only kinds whose execution matches this lane appear here.
    # The lanes are provably self-contained (every hpc kind depends only on upstream
    # hpc kinds, every runner kind only on runner kinds), so the filtered kind list
    # yields a self-consistent single-lane DAG — the per-kind `if:` filter and
    # `from-jobs` matching are unchanged.
    runnable = sorted(k for k, mk in m.matrices.items() if mk.triggers and mk.execution == lane)
    if not runnable:
        return None

    cross_needs_per_kind: dict[str, list[JobRef]] = {k: transitive_cross_repo_needs(m, k, by_pkg) for k in runnable}
    union_cross: list[JobRef] = []
    seen: set[tuple[str, str]] = set()
    for k in runnable:
        for r in cross_needs_per_kind[k]:
            key = (r.package, r.kind)
            if key not in seen:
                seen.add(key)
                union_cross.append(r)

    jobs: dict[str, Any] = {"resolve": _resolve_job(m, runnable, union_cross)}
    for kind in runnable:
        jid = job_id(m.package_name, kind)
        jobs[jid] = _kind_job(m, kind, cross_needs_per_kind[kind])

    doc: dict[str, Any] = {
        "name": f"Cross-repo trigger ({m.package_name})",
        # run-name embeds dispatch-id so the workflow_dispatch recovery path
        # (resolve_deps) can correlate this run with the dispatch it just made.
        # Inert under workflow_call: GitHub names the run after the top-level
        # caller and ignores a called workflow's run-name, so the empty
        # dispatch-id there does no harm.
        "run-name": ("Cross-repo trigger (${{ inputs.from-repo }}@${{ inputs.from-sha }}) [${{ inputs.dispatch-id }}]"),
        # Two entry points share the same typed inputs:
        #   - workflow_call: the upstream's trigger-downstream.yml orchestrator
        #     invokes us as a reusable workflow, so GitHub waits for completion
        #     natively (no API polling) and our jobs show on the upstream PR.
        #   - workflow_dispatch: resolve_deps' consumer-driven recovery still
        #     fires us via `gh workflow run` when an upstream artifact is missing.
        # dispatch-id is dispatch-only (run-name correlation); workflow_call
        # never sets it, so it is optional/defaulted.
        "on": {
            "workflow_call": {"inputs": _workflow_call_inputs()},
            "workflow_dispatch": {"inputs": _workflow_dispatch_inputs()},
        },
        # Concurrency keyed on the producer package + ref, via a STATIC token —
        # NOT `github.workflow`. Under workflow_call (the orchestrator path)
        # github.workflow/github.ref resolve to the *caller's* values, so a
        # `${{ github.workflow }}-${{ github.ref }}` group would collapse onto
        # the top-level caller's own group and GitHub would cancel us with a
        # "deadlock detected" (parent waits for us; we wait for the parent's
        # group). A package-specific literal can never collide with a caller.
        # Still coalesces simultaneous consumer-driven dispatches from C and E
        # into B onto one run; we don't cancel in-flight so a late dispatcher
        # reuses the result instead of restarting.
        "concurrency": {
            "group": f"cross-repo-trigger-{lane}-{m.package_name}-" + "${{ github.ref }}",
            "cancel-in-progress": False,
        },
        # The object-store location and the AWS creds (one credential pair, used
        # by both the artifact store and sccache) flow to every job so resolve's
        # S3 existence checks and each kind's fetch/publish reach the artifact
        # store. Workflow-level so we never miss a job as kinds are added.
        #
        # SCCACHE_BUCKET rides along because setup-sccache takes its bucket from
        # the job environment and has no fallback for it -- deliberately, since
        # artifacts and sccache are orthogonal uses in SEPARATE buckets and one
        # must never silently borrow the other's. Without it here, any downstream
        # build calling setup-sccache with backend=s3 fails outright. The
        # endpoint needs no entry: the action falls back to the artifact one,
        # because that half really is shared.
        "env": {
            "ARTIFACT_POLL_INTERVAL": "${{ vars.ARTIFACT_POLL_INTERVAL || '60' }}",
            "ARTIFACT_S3_ENDPOINT": "${{ secrets.ARTIFACT_S3_ENDPOINT }}",
            "ARTIFACT_S3_BUCKET": "${{ secrets.ARTIFACT_S3_BUCKET }}",
            "SCCACHE_BUCKET": "${{ secrets.SCCACHE_BUCKET }}",
            "AWS_ACCESS_KEY_ID": "${{ secrets.AWS_ACCESS_KEY_ID }}",
            "AWS_SECRET_ACCESS_KEY": "${{ secrets.AWS_SECRET_ACCESS_KEY }}",
        },
        "jobs": jobs,
    }
    return GENERATED_HEADER + _dump_workflow(doc)


def _shared_trigger_inputs() -> dict[str, dict[str, Any]]:
    """Inputs common to both entry points (workflow_call + workflow_dispatch):
    dispatcher metadata + branch-match hints.

    `from-jobs` carries the upstream-change coordinates (a JSON array of
    package/kinds); a consumer asking us to rebuild our artifact sends
    `rebuild-request=true` instead. The two are mutually exclusive (exactly one
    is set per invocation); the per-kind `if:` filter enforces it. One dispatch
    carries every originator kind that reaches us, so both a runner chain and an
    HPC chain wake from a single call — the per-kind `if:` matches the array with
    `contains(fromJSON(...))`.
    """
    return {
        "from-repo": {
            "description": "owner/repo of the dispatcher (upstream producer or downstream consumer)",
            "required": True,
            "type": "string",
        },
        "from-sha": {
            "description": "SHA of the dispatcher commit",
            "required": True,
            "type": "string",
        },
        "from-jobs": {
            "description": (
                "JSON array of package/kinds that triggered us (e.g. "
                '\'["fortmath/build","fortmath/build-hpc"]\') for upstream-change dispatches. '
                "'[]' when rebuild-request is true."
            ),
            "required": False,
            "default": "[]",
            "type": "string",
        },
        "rebuild-request": {
            "description": (
                "True when a downstream consumer dispatches us because our artifact was "
                "missing. Mutually exclusive with from-jobs."
            ),
            "required": False,
            "default": False,
            "type": "boolean",
        },
        "branch": {
            "description": "Dispatcher branch name; we attempt branch-matching against it",
            "required": True,
            "type": "string",
        },
        "fallback-ref": {
            "description": "Ref to check out if branch does not exist in this repo",
            "required": False,
            "type": "string",
            "default": "main",
        },
    }


def _workflow_call_inputs() -> dict[str, dict[str, Any]]:
    """workflow_call typed inputs: just the shared set. The orchestrator passes
    these via `with:`; GitHub waits for the called run natively, so there is no
    dispatch-id (no run-name correlation needed)."""
    return _shared_trigger_inputs()


def _workflow_dispatch_inputs() -> dict[str, dict[str, Any]]:
    """workflow_dispatch typed inputs: the shared set plus `dispatch-id`, which
    resolve_deps' recovery stamps into run-name so it can correlate the run it
    just fired. Optional/defaulted so the workflow_call entry point (which never
    sets it) stays valid."""
    return {
        "dispatch-id": {
            "description": "Correlation id stamped into run-name so the dispatcher can find this run",
            "required": False,
            "default": "",
            "type": "string",
        },
        **_shared_trigger_inputs(),
    }


def _render_kind_filter(refs: Sequence[JobRef], triggers: Iterable[str]) -> str:
    """Build the `if:` expression gating a job on the dispatch trigger conditions.

    Each opted-in trigger contributes its own clause:

      - `upstream-change`: one `contains(fromJSON(inputs.from-jobs), 'pkg/kind')`
        per ref in `refs`. `from-jobs` is a JSON array, so a single dispatch can
        carry several originator kinds and array-`contains` matches whole
        elements (``fortmath/build`` never matches ``fortmath/build-hpc``).
      - `rebuild-request`: a single `inputs.rebuild-request` clause.

    The result is the OR of all clauses. An empty triggers set produces
    `"false"` — the job would never fire — but the caller should be gating
    rendering itself rather than relying on this.
    """
    triggers_set = frozenset(triggers)
    clauses: list[str] = []
    if TRIGGER_UPSTREAM_CHANGE in triggers_set:
        clauses.extend(f"contains(fromJSON(inputs.from-jobs), '{r.package}/{r.kind}')" for r in refs)
    if TRIGGER_REBUILD_REQUEST in triggers_set:
        clauses.append("inputs.rebuild-request")
    return " || ".join(clauses) if clauses else "false"


def _mint_step() -> Step:
    """The `create-github-app-token` mint step that every cross-repo job
    starts with. GA redacts add-mask'd values when they flow through
    needs.<job>.outputs.* (community/13082), so jobs can't share a token via
    outputs; each one mints its own.
    """
    return {
        "id": "mint",
        "uses": "actions/create-github-app-token@v3",
        "with": {
            "client-id": "${{ secrets.CI_PERMISSIONS_APP_CLIENT_ID }}",
            "private-key": "${{ secrets.CI_PERMISSIONS_APP_PRIVATE_KEY }}",
            "owner": "${{ github.repository_owner }}",
        },
    }


def _resolve_job(m: Manifest, runnable: Sequence[str], cross: Sequence[JobRef]) -> dict[str, Any]:
    matrix_arg = ",".join(runnable)
    # Resolve fires whenever any runnable kind would; its filter is the union
    # of all kinds' triggers.
    all_triggers: frozenset[str] = frozenset().union(*(m.matrices[k].triggers for k in runnable))
    cond = _render_kind_filter(cross, all_triggers)
    outputs: dict[str, str] = {"ref": "${{ steps.pick.outputs.ref }}"}
    for kind in runnable:
        outputs[f"matrix-{kind}"] = f"${{{{ steps.r.outputs.matrix-{kind} }}}}"
    return {
        "if": cond,
        "runs-on": SLIM_RUNNER,
        "outputs": outputs,
        "steps": [
            _mint_step(),
            {
                "id": "pick",
                "uses": "ecmwf/ci-infrastructure/actions/pick-ref@main",
                "with": {
                    "repo": m.repo,
                    "try-branch": "${{ inputs.branch }}",
                    "fallback-ref": "${{ inputs.fallback-ref }}",
                    "token": "${{ steps.mint.outputs.token }}",
                },
            },
            {
                # Explicit repository + token: under workflow_call this workflow
                # runs in the CALLER's run, so github.repository is the upstream
                # orchestrator's repo, not ours. A bare checkout would clone the
                # caller and resolve-deps would read the WRONG manifest. Pin to
                # our own repo at the branch-matched ref.
                "uses": "actions/checkout@v6",
                "with": {
                    "repository": m.repo,
                    "ref": "${{ steps.pick.outputs.ref }}",
                    "token": "${{ steps.mint.outputs.token }}",
                },
            },
            {
                "id": "r",
                "uses": "ecmwf/ci-infrastructure/actions/resolve-deps@main",
                "with": {
                    "current-branch": "${{ steps.pick.outputs.ref }}",
                    "matrix": matrix_arg,
                    "token": "${{ steps.mint.outputs.token }}",
                    # App credentials enable consumer-driven recovery: if any transitive
                    # dep's artifact is missing on a normal ref, resolve-deps dispatches
                    # that producer's cross-repo-trigger.yml and waits.
                    "client-id": "${{ secrets.CI_PERMISSIONS_APP_CLIENT_ID }}",
                    "app-private-key": "${{ secrets.CI_PERMISSIONS_APP_PRIVATE_KEY }}",
                },
            },
        ],
    }


def _field_to_var(name: str) -> str:
    """Convert a hyphenated matrix-leg field name to a shell-safe variable name.

    `cxx-compiler` -> `cxx_compiler`, `runs-on` -> `runs_on`, etc. The
    GITHUB_OUTPUT key keeps the hyphenated form (matching the original field
    name) so the action call's `with:` block stays readable.
    """
    return name.replace("-", "_")


def _decode_step(mk: MatrixKind) -> Step:
    """`Decode matrix-leg` step — extracts mk.forwarded_inputs +
    _resolved.own-artifact-name + _resolved.deps from the matrix leg JSON,
    fails loudly on empty/null via the require helper, writes outputs.
    """
    # A forwarded field that is absent from at least one leg (e.g. `options`, which a
    # plain build omits) is OPTIONAL: extract it with `// ""` and ALLOW empty. Fields
    # present in every leg are mandatory and keep the require() non-empty guard.
    optional_fields = {fld for fld in mk.forwarded_inputs if any(fld not in leg for leg in mk.legs)}
    var_assignments: list[str] = []
    for fld in mk.forwarded_inputs:
        var = _field_to_var(fld)
        if fld in optional_fields:
            var_assignments.append(f'{var}=$(jq -r \'."{fld}" // ""\' <<<"$leg")')
        else:
            var_assignments.append(f"{var}=$(require '.\"{fld}\"' {fld})")
    var_assignments.append("deps_json=$(require '._resolved.deps | tojson' '_resolved.deps')")
    var_assignments.append(
        "own_artifact_name=$(require '._resolved.\"own-artifact-name\"' '_resolved.own-artifact-name')"
    )

    output_lines: list[str] = []
    for fld in mk.forwarded_inputs:
        output_lines.append(f'  echo "{fld}=${{{_field_to_var(fld)}}}"')
    output_lines.append('  echo "deps-json=${deps_json}"')
    output_lines.append('  echo "own-artifact-name=${own_artifact_name}"')

    body_lines = [
        "set -euo pipefail",
        "shopt -s inherit_errexit 2>/dev/null || true",
        "command -v jq >/dev/null || {",
        '  echo "::error::jq is required but not installed in this runner/image." >&2',
        "  exit 1",
        "}",
        "leg='${{ toJSON(matrix) }}'",
        "require() {",
        "  local val",
        '  val=$(jq -r "$1" <<<"$leg")',
        '  if [ -z "$val" ] || [ "$val" = "null" ]; then',
        "    echo \"::error::matrix-leg field '$2' empty or null (jq -r '$1' returned '$val').\" >&2",
        "    exit 1",
        "  fi",
        "  printf '%s' \"$val\"",
        "}",
        *var_assignments,
        "{",
        *output_lines,
        '} >> "$GITHUB_OUTPUT"',
    ]
    return {
        "name": "Decode matrix-leg",
        "id": "m",
        "shell": "bash",
        "run": _BlockScalar("\n".join(body_lines) + "\n"),
    }


def _setup_python_step(mk: MatrixKind) -> Step | None:
    """`actions/setup-python@v6` between Decode and Fetch — emitted only when
    a leg declares python-version. Provides the right interpreter to
    fetch_deps.py's pip-install of needs-python wheels (the kind's composite
    runs after Fetch, so its own setup-python step is too late).
    """
    if not any("python-version" in leg for leg in mk.legs):
        return None
    return {
        "name": "Set up Python ${{ steps.m.outputs.python-version }}",
        "uses": "actions/setup-python@v6",
        "with": {"python-version": "${{ steps.m.outputs.python-version }}"},
    }


def _action_call_step(mk: MatrixKind) -> Step:
    """The `uses: <manifest.action>` step with forwarded inputs. When
    `publishes` is false (test kinds), `own-artifact-name` is auto-added so
    the action can download the artifact it's verifying.
    """
    with_block: dict[str, str] = {}
    for out in mk.forwarded_deps_outputs:
        with_block[out] = f"${{{{ steps.deps.outputs.{out} }}}}"
    for fld in mk.forwarded_inputs:
        with_block[fld] = f"${{{{ steps.m.outputs.{fld} }}}}"
    if not mk.publishes:
        with_block["own-artifact-name"] = "${{ steps.m.outputs.own-artifact-name }}"
    return {
        "name": "Build" if mk.publishes else "Run",
        "id": "build",
        "uses": mk.action,
        "with": with_block,
    }


def _hpc_build_step(mk: MatrixKind) -> Step:
    """The build-on-hpc step for an HPC kind — a drop-in for `_action_call_step`.

    Consumes the resolved cmake-prefix-path from the download-only step and
    exposes `install-path` (id: build) exactly like the runner build composite,
    so the surrounding job wiring (publish, print-dep-table, …) is unchanged.
    Publishing happens inside build-on-hpc (gated on cache-hit), so `_kind_job`
    emits no separate publish step for HPC kinds.

    `site` is a scheduling field taken from the matrix leg; `job-script` is the
    repo-owned recipe. A multi-recipe kind (e.g. one leg per Python version)
    names it per leg, which overrides the kind-level default; a leg that declares
    none falls back to that default. `work-dir` and `troika-user` come from
    org-level config (a variable and a secret) so the cluster paths and ssh
    identity aren't baked into every manifest.

    A test-only kind (`publishes = false`) builds and publishes no artifact, so
    it runs build-on-hpc with `publish: false`: the job runs for its pass/fail
    sentinel and the never-created install tree is neither fetched nor published.
    """
    assert mk.job_script is not None  # guaranteed for execution == "hpc"
    with_block: dict[str, str] = {
        "site": "${{ matrix.site }}",
        "job-script": f"${{{{ matrix.job-script || '{mk.job_script}' }}}}",
        "artifact-name": "${{ steps.m.outputs.own-artifact-name }}",
        "cmake-prefix-path": "${{ steps.deps.outputs.cmake-prefix-path }}",
        "work-dir": "${{ vars.HPC_CI_WORK_DIR }}",
        "remote-work-dir": "${{ vars.HPC_CI_REMOTE_WORK_DIR }}",
        "troika-user": "${{ secrets.HPC_CI_SSH_USER }}",
    }
    if not mk.publishes:
        with_block["publish"] = "false"
    return {
        "name": "Build on HPC" if mk.publishes else "Run on HPC",
        "id": "build",
        "uses": "ecmwf/ci-infrastructure/actions/build-on-hpc@main",
        "with": with_block,
    }


# A per-kind job posts a check run back to the dispatcher only on the
# workflow_dispatch entry point with upstream-change coordinates (from-jobs set).
# That is exactly the "a public upstream dispatched us because we are private"
# case: reusable-workflow invocations surface as push/pull_request events (not
# workflow_dispatch) and already show on the PR natively, and the rebuild-request
# recovery path leaves from-jobs empty. See render_orchestrator_workflow.
_CHECK_RUN_WHEN: Final = "github.event_name == 'workflow_dispatch' && inputs.from-jobs != '[]'"
# The dispatched run lives in THIS repo; under workflow_dispatch github.repository
# is us (the private consumer), so the URL points at the private run (auth-gated).
_CHECK_RUN_DETAILS_URL: Final = "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
_REPORT_CHECK_RUN_ACTION: Final = "ecmwf/ci-infrastructure/actions/report-check-run@main"


def _check_run_start_step(check_name: str) -> Step:
    """Post an in-progress check run to the dispatcher's commit (start of a
    dispatched per-kind job). No-op unless a public upstream dispatched us."""
    return {
        "id": "check_start",
        "if": "${{ " + _CHECK_RUN_WHEN + " }}",
        "uses": _REPORT_CHECK_RUN_ACTION,
        "with": {
            "token": "${{ steps.mint.outputs.token }}",
            "head-repo": "${{ inputs.from-repo }}",
            "head-sha": "${{ inputs.from-sha }}",
            "name": check_name,
            "details-url": _CHECK_RUN_DETAILS_URL,
            "phase": "start",
        },
    }


def _check_run_finish_step(check_name: str) -> Step:
    """Complete the check run with this job's conclusion. `always()` so a failed
    or cancelled build still reports; conclusion is taken from job.status."""
    return {
        "name": "Report check-run conclusion",
        "if": "${{ always() && " + _CHECK_RUN_WHEN + " }}",
        "uses": _REPORT_CHECK_RUN_ACTION,
        "with": {
            "token": "${{ steps.mint.outputs.token }}",
            "head-repo": "${{ inputs.from-repo }}",
            "head-sha": "${{ inputs.from-sha }}",
            "name": check_name,
            "details-url": _CHECK_RUN_DETAILS_URL,
            "phase": "finish",
            "conclusion": "${{ job.status }}",
            "check-run-id": "${{ steps.check_start.outputs.check-run-id }}",
        },
    }


def _kind_job(m: Manifest, kind: str, cross: Sequence[JobRef]) -> dict[str, Any]:
    """One job per kind: mint → checkout → decode → (setup-python) → fetch →
    action call → (publish). The action call is the manifest-declared
    composite (e.g. build-cxxmath); the per-job dispatch shim that used to
    live in each repo's downstream-job composite is inlined here.

    container.image is driven by `${{ matrix.container || '' }}`: when the
    leg sets a container the job runs in that image, when it doesn't the
    expression evaluates to '' and GA falls back to host mode.
    """
    mk = m.matrices[kind]
    display = f"{m.package_name}/{kind}"

    local_needs = [n for n in mk.needs if "/" not in n]
    needs_list = ["resolve"] + [job_id(m.package_name, n) for n in local_needs]

    cond = _render_kind_filter(cross, mk.triggers)
    distinguishing = _first_distinguishing_field(mk.legs) or "runs-on"
    # Build the display slots left-to-right: the distinguishing leg field, then a
    # py<version> slot whenever the legs carry a python-version (so a job that
    # varies python shows it even when another field is the primary
    # distinguisher — mirrors the hand-written ci.yml names, e.g.
    # "build+test (clang++-18, py3.11, ubuntu-24.04)"), then the platform.
    # platform and python-version get dedicated handling so neither is duplicated
    # when it is itself the distinguisher.
    slots: list[str] = []
    if distinguishing not in ("platform", "python-version"):
        expr = f"matrix['{distinguishing}']" if "-" in distinguishing else f"matrix.{distinguishing}"
        slots.append(f"${{{{ {expr} }}}}")
    if any("python-version" in leg for leg in mk.legs):
        slots.append("py${{ matrix.python-version }}")
    slots.append("${{ matrix.platform }}")
    job_name = f"{display} ({', '.join(slots)})"

    steps: list[Step] = [
        _mint_step(),
        # Signal "in progress" on the dispatcher's PR before doing any work, so a
        # private consumer of a public upstream shows a pending check immediately.
        _check_run_start_step(job_name),
        {
            # Explicit repository + token: under workflow_call github.repository
            # is the caller's, so a bare checkout would clone the upstream
            # orchestrator's repo instead of ours. Pin to our own repo.
            "uses": "actions/checkout@v6",
            "with": {
                "repository": m.repo,
                "ref": "${{ needs.resolve.outputs.ref }}",
                "token": "${{ steps.mint.outputs.token }}",
            },
        },
        _decode_step(mk),
    ]
    is_hpc = mk.execution == EXECUTION_HPC
    # setup-python provides the interpreter fetch_deps installs needs-python
    # wheels into; HPC legs build inside a SLURM job and never take that path.
    if not is_hpc:
        setup_py = _setup_python_step(mk)
        if setup_py is not None:
            steps.append(setup_py)
    fetch_with = {
        "mode": "download-only",
        "deps-json": "${{ steps.m.outputs.deps-json }}",
        "token": "${{ steps.mint.outputs.token }}",
    }
    if is_hpc:
        # No setup-python ran above, so there is no consumer interpreter to
        # install needs-python wheels into — and installing them on the login
        # node would be pointless anyway. Stage them; the repo's job script
        # installs them on the compute node from $CMAKE_PREFIX_PATH.
        fetch_with["install-python-deps"] = "false"
    steps.append(
        {
            "name": "Fetch resolved deps",
            "id": "deps",
            "uses": "ecmwf/ci-infrastructure/actions/fetch-and-publish@main",
            "with": fetch_with,
        }
    )
    if is_hpc:
        # build-on-hpc submits the SLURM job AND publishes internally (gated on
        # cache-hit), so no separate publish step follows.
        steps.append(_hpc_build_step(mk))
    else:
        steps.append(_action_call_step(mk))
        if mk.publishes:
            steps.append(
                {
                    "name": "Publish",
                    "uses": "ecmwf/ci-infrastructure/actions/fetch-and-publish@main",
                    "with": {
                        "mode": "publish",
                        "install-path": "${{ steps.build.outputs.install-path }}",
                        "artifact-name": "${{ steps.m.outputs.own-artifact-name }}",
                    },
                }
            )
    # Last step: report the final conclusion back to the dispatcher (always()).
    steps.append(_check_run_finish_step(job_name))

    job: dict[str, Any] = {
        "needs": needs_list,
        "if": cond,
        "name": job_name,
        "runs-on": "${{ matrix['runs-on'] }}",
    }
    # HPC jobs run on the login-node self-hosted runner in host mode; only the
    # runner path threads a container image.
    if not is_hpc:
        job["container"] = {"image": "${{ matrix.container || '' }}"}
    job["strategy"] = {
        "fail-fast": False,
        "matrix": f"${{{{ fromJSON(needs.resolve.outputs.matrix-{kind}) }}}}",
    }
    job["steps"] = steps
    return job


_DEFER_DISPLAY: Final = frozenset({"container", "runs-on", "site"})


def _first_distinguishing_field(legs: Sequence[dict[str, Any]]) -> str | None:
    """Pick a matrix field whose values vary across legs; used in the job display name.

    Compiler / python-version fields are preferred — they make the most useful
    primary label. `platform` is the next-best fallback (it already appears in
    the name suffix, but a short distro string beats a long image tag). Finally
    `container` and `runs-on` are deprioritised hardest: a full image string
    makes a noisy primary label.
    """
    if not legs:
        return None
    keys = set(legs[0].keys())

    def sort_key(k: str) -> tuple[int, str]:
        if k in _DEFER_DISPLAY:
            return (2, k)
        if k == "platform":
            return (1, k)
        return (0, k)

    for k in sorted(keys, key=sort_key):
        # A leg value may be a list (e.g. runs-on = ["self-hosted", "linux", "hpc"]),
        # which is unhashable; normalise to a tuple so the set can dedup it.
        vals = {tuple(v) if isinstance(v, list) else v for v in (leg.get(k) for leg in legs)}
        if len(vals) > 1:
            return k
    # All legs identical: pick anything but still respect the deferral.
    return next(iter(sorted(keys, key=sort_key)), None)


def compute_transitive_consumers(
    manifests: Sequence[Manifest],
) -> dict[str, dict[str, dict[str, list[str]]]]:
    """For each manifest, return a per-repo mapping of `<package>/<kind>` to a
    dict with two fields:

      - `consumers`: sorted list of transitive consumer `owner/repo`s (the
        orchestrator's flat fan-out target list).
      - `expected-checks`: sorted list of `<package>/<kind>` job names that
        the orchestrator emits as caller-side jobs in trigger-downstream.yml.

    A `<package>/<kind>` key is included only when some other manifest references
    it as a cross-repo `needs` target -- i.e. when the upstream has real
    consumers to fan out to. Repos with nothing downstream get an empty dict.

    Closure rule: when this job changes, every repo *transitively* downstream of
    us (via the `[[trigger-downstream]]` graph, starting at the direct consumers
    that name this job in their `needs`) needs a re-test. We collapse the closure
    at generation time so the orchestrator can call every consumer flat (depth=1)
    rather than via chained workflow_call (which hits GHA's 4-deep cap).
    """
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    # 1) Find direct consumers per (package, kind): which repos cite this job in needs?
    direct: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in manifests:
        for mk in m.matrices.values():
            for need in mk.needs:
                pkg, target_kind = _split_need(need)
                if pkg is None:
                    continue
                # Validation has already verified the upstream exists; pkg is in by_pkg.
                direct[(pkg, target_kind)].add(m.repo)

    # 2) Trigger graph (repo -> repos it triggers). Targets outside our manifest set
    # (e.g. an external `stack-deps`) are silently dropped from the closure -- the
    # orchestrator can't `uses:` a workflow file we don't own.
    trigger_out: dict[str, list[str]] = {m.repo: [t.repo for t in m.triggers if t.repo in by_repo] for m in manifests}

    # 3) BFS the trigger graph from each direct-consumer set to obtain the closure.
    out: dict[str, dict[str, dict[str, list[str]]]] = {m.repo: {} for m in manifests}
    for (pkg, kind), starts in direct.items():
        upstream = by_pkg.get(pkg)
        if upstream is None:
            continue
        seen: set[str] = set()
        q: deque[str] = deque(starts)
        while q:
            node = q.popleft()
            if node in seen:
                continue
            seen.add(node)
            for nxt in trigger_out.get(node, []):
                if nxt not in seen:
                    q.append(nxt)

        # 4) For every consumer in the closure, list the kinds whose
        # upstream-job filter actually accepts `pkg/kind` -- those are the
        # caller-side jobs the orchestrator will emit.
        upstream_ref = (pkg, kind)
        expected: list[str] = []
        for consumer_repo in seen:
            consumer = by_repo[consumer_repo]
            for c_kind, mk in consumer.matrices.items():
                if TRIGGER_UPSTREAM_CHANGE not in mk.triggers:
                    continue
                refs = transitive_cross_repo_needs(consumer, c_kind, by_pkg)
                if any((r.package, r.kind) == upstream_ref for r in refs):
                    expected.append(f"{consumer.package_name}/{c_kind}")

        out[upstream.repo][f"{pkg}/{kind}"] = {
            "consumers": sorted(seen),
            "expected-checks": sorted(expected),
        }
    return out


def resolve_consumer_refs(m: Manifest, by_repo: Mapping[str, Manifest]) -> dict[str, str]:
    """Walk m's transitive trigger closure and return {consumer-repo: agreed-ref}.

    For each consumer reachable via the [[trigger-downstream]] graph, the
    incoming-edge ref(s) must agree. If two distinct paths reach the same
    consumer with different refs, raise SchemaError -- the orchestrator can't
    pin `uses: …@<ref>` to two values.

    Direct triggers contribute the ref from m's own [[trigger-downstream]];
    transitive triggers contribute the ref from intermediate parents'
    [[trigger-downstream]] entries.
    """
    refs: dict[str, str] = {}
    seen: set[str] = set()
    queue: deque[str] = deque([m.repo])
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        parent = by_repo.get(node)
        if parent is None:
            continue
        for t in parent.triggers:
            if t.repo not in by_repo:
                # External trigger; no orchestrator job will be emitted for it.
                continue
            existing = refs.get(t.repo)
            if existing is None:
                refs[t.repo] = t.ref
            elif existing != t.ref:
                raise SchemaError(
                    f"transitive ref disagreement for consumer {t.repo}: "
                    f"got both {existing!r} and {t.ref!r} via different paths from {m.repo}"
                )
            queue.append(t.repo)
    return refs


# Three GitHub Actions limits bound each lane's orchestrator; we enforce each at
# generation time so a manifest change that breaches one fails at PR review,
# not at workflow-load. Every lane is its own top-level `workflow_run`, so these
# budgets apply per lane (post-split counts only shrink):
#
#   1. 256 jobs per run. The consumer's resolve+legs expand INSIDE the
#      orchestrator's run (each reusable-workflow call is part of the same run, not a
#      separately-dispatched one), so they count here. ORCHESTRATOR_MAX_TOTAL_JOBS
#      is a tighter safety margin; _check_orchestrator_caps sums the expansion.
#   2. 4 nested workflow levels. Our chain is now trigger-downstream{-hpc} (L1,
#      top-level workflow_run) -> consumer cross-repo-trigger{-hpc} (L2). The flat
#      closure (compute_transitive_consumers) guarantees the consumer workflow never
#      itself `uses:` another reusable workflow — it only calls composite actions,
#      which don't count toward the cap — so depth is fixed at 2.
#   3. 20 distinct reusable workflows called transitively from one top-level
#      caller. Each consumer package's cross-repo-trigger.yml is one distinct
#      file, so the count equals the number of consumer packages in the closure.
ORCHESTRATOR_MAX_TOTAL_JOBS: Final = 220
ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS: Final = 20


def render_orchestrator_workflow(
    m: Manifest,
    by_pkg: Mapping[str, Manifest],
    by_repo: Mapping[str, Manifest],
    closures: Mapping[str, dict[str, dict[str, list[str]]]],
    *,
    lane: Execution,
) -> str | None:
    """Emit `.github/workflows/trigger-downstream{-hpc}.yml` for one upstream repo
    and one lane, or None if `m` has no in-lane transitive consumers.

    Top-level `on: workflow_run` (fires when the upstream's `CI` workflow completes),
    NOT a nested `workflow_call` from ci.yml. Each lane is its own Actions-tab run
    with its own 256-job / 20-reusable-workflow budget, so the two lanes never share
    a run and the graphs stay short. Root jobs gate on `workflow_run.conclusion ==
    'success'`, so downstream now waits for the upstream's full CI (tests included) —
    a deliberate relaxation of "depend only on the build step" for clean separate runs.

    Flat fan-out: one orchestrator job per in-lane consumer package. Each job invokes
    the consumer's cross-repo-trigger{-hpc}.yml as a reusable workflow (workflow_call)
    with `secrets: inherit`; GitHub waits for it natively and surfaces its jobs on the
    Actions run. Cross-package needs are encoded as orchestrator-side `needs:` so
    cross-repo build order is preserved; the called workflow handles intra-package kind
    ordering internally. All ordering is same-lane only (see _cross_package_deps).

    The one exception is a PUBLIC upstream fanning out to a PRIVATE consumer
    (_edge_needs_dispatch): that job dispatches the consumer instead of calling it,
    keeping the private repo's logs out of this public run. See _orchestrator_dispatch_job.

    Because plain `workflow_run` runs post nothing to the PR, extra jobs post a commit
    status (`downstream/{lane}`) back to the tested head SHA — `report-start` (pending)
    and `report-result` (final) on the success path, and `report-ci-failure` (a red
    status) when the upstream CI did not succeed. Exactly one final status is posted per
    completed CI run, so the context is never left hanging and can be a required check
    that blocks the merge.

    Branch matching is delegated to the called workflow's `pick-ref` step at runtime;
    the orchestrator passes `branch` (the upstream's head branch) and `fallback-ref`
    (the manifest-declared consumer ref). The `uses:@<ref>` itself is pinned to the
    static manifest ref (GHA forbids expressions there), which only selects the workflow
    *definition* — the code built is still the branch-matched checkout from pick-ref.
    """
    self_closure = closures.get(m.repo, {})
    if not self_closure:
        return None

    consumer_refs = resolve_consumer_refs(m, by_repo)

    # Every in-lane originator kind that reaches each consumer. `from-jobs` carries the
    # whole set, so ONE call per consumer wakes all its (same-lane) chains. Only
    # originator kinds whose execution matches this lane contribute — the produced file
    # is a single-lane DAG.
    origins: dict[str, set[str]] = {}  # cpkg -> the in-lane orig_keys reaching it
    for orig_key in sorted(self_closure.keys()):
        okind = orig_key.split("/", 1)[1]
        if m.matrices[okind].execution != lane:
            continue
        for r in self_closure[orig_key]["consumers"]:
            origins.setdefault(by_repo[r].package_name, set()).add(orig_key)

    if not origins:
        # This upstream has consumers, but none reached via an in-lane originator kind
        # (e.g. a runner-only producer has no hpc orchestrator).
        return None

    _check_orchestrator_caps(m, self_closure, by_pkg, by_repo, lane=lane)

    all_consumers = sorted(origins)
    # Cross-package ordering, collapsed across kinds but restricted to this lane: a
    # consumer waits for the whole of each same-lane upstream consumer it depends on.
    consumer_deps = _cross_package_deps(all_consumers, by_pkg, lane=lane)

    # `validate` runs first and gates every consumer dispatch: if this repo's manifest
    # and its checked-in workflow YAMLs have drifted, fail immediately rather than
    # spending dispatch tokens and consumer-side build time on a stale orchestrator.
    jobs: dict[str, Any] = {
        "validate": _validate_job(),
        "report-start": _report_start_job(lane),
        "report-ci-failure": _report_ci_failure_job(lane),
    }
    consumer_job_ids: list[str] = []
    for cpkg in all_consumers:
        cmanifest = by_pkg[cpkg]
        crepo = cmanifest.repo
        cref = consumer_refs[crepo]
        jid = _orchestrator_job_id(cpkg)
        consumer_job_ids.append(jid)
        dep_ids = sorted(_orchestrator_job_id(p) for p in consumer_deps.get(cpkg, set()))
        from_jobs = sorted(origins[cpkg])
        # Log-isolation rule: a public upstream must NOT reach a private consumer via
        # `uses:` (reusable-workflow jobs run in the caller's public run, exposing the
        # private repo's build logs). Dispatch it instead, so its run — and logs — stay
        # in the private repo; this job then waits on the downstream run's conclusion
        # (status only) so it gates like a workflow_call edge. Every other edge keeps the
        # native reusable-workflow call.
        if _edge_needs_dispatch(m, cmanifest):
            jobs[jid] = _orchestrator_dispatch_job(cpkg, crepo, cref, from_jobs, dep_ids, lane=lane)
        else:
            jobs[jid] = _orchestrator_job(cpkg, crepo, cref, from_jobs, dep_ids, lane=lane)

    jobs["report-result"] = _report_result_job(lane, consumer_job_ids)

    lane_label = "runner" if lane == EXECUTION_RUNNER else "HPC"
    doc: dict[str, Any] = {
        "name": f"Downstream {lane_label} ({m.package_name})",
        # Top-level workflow_run: fires when this repo's CI completes. All ci.yml are
        # named `CI`. Root jobs additionally gate on conclusion == 'success', so a
        # failed CI posts nothing and the required downstream/<lane> status stays
        # "Expected" — blocking the merge.
        "on": {
            "workflow_run": {
                "workflows": ["CI"],
                "types": ["completed"],
            },
        },
        # Coalesce re-runs for the same tested commit; a superseding run cancels the
        # in-flight one. Keyed by lane so the two lanes' runs never collide.
        "concurrency": {
            "group": f"trigger-downstream-{lane}-" + "${{ github.event.workflow_run.head_sha }}",
            "cancel-in-progress": True,
        },
        "jobs": jobs,
    }
    return GENERATED_HEADER + _dump_workflow(doc)


_SUCCESS_GATE: Final = "${{ github.event.workflow_run.conclusion == 'success' }}"


def _report_start_job(lane: Execution) -> dict[str, Any]:
    """Post a `pending` commit status for `downstream/{lane}` to the tested head SHA,
    before any consumer starts. Gated on CI success so nothing posts when CI failed."""
    context = _status_context(lane)
    lane_label = "runner" if lane == EXECUTION_RUNNER else "HPC"
    script = (
        "gh api -X POST \\\n"
        '  "/repos/${{ github.repository }}/statuses/${{ github.event.workflow_run.head_sha }}" \\\n'
        "  -f state=pending \\\n"
        f"  -f context='{context}' \\\n"
        '  -f target_url="${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" \\\n'
        f"  -f description='Downstream {lane_label} tests running'\n"
    )
    return {
        "if": _SUCCESS_GATE,
        "runs-on": SLIM_RUNNER,
        "steps": [
            _mint_step(),
            {
                "name": "Post pending downstream status",
                "env": {"GH_TOKEN": "${{ steps.mint.outputs.token }}"},
                "run": _BlockScalar(script),
            },
        ],
    }


def _report_ci_failure_job(lane: Execution) -> dict[str, Any]:
    """Post a `failure` commit status for `downstream/{lane}` when the upstream CI did
    NOT succeed. This is the exact complement of the success gate every other root job
    carries, so precisely one status is posted per completed CI run: report-start/-result
    on success, this on anything else. Without it a failed CI would post nothing and a
    required `downstream/{lane}` check would hang at "Expected" instead of going red.

    `target_url` points at the failed CI run itself (`workflow_run.html_url`), since this
    orchestrator run does no downstream work worth linking to."""
    context = _status_context(lane)
    lane_label = "runner" if lane == EXECUTION_RUNNER else "HPC"
    script = (
        "gh api -X POST \\\n"
        '  "/repos/${{ github.repository }}/statuses/${{ github.event.workflow_run.head_sha }}" \\\n'
        "  -f state=failure \\\n"
        f"  -f context='{context}' \\\n"
        '  -f target_url="${{ github.event.workflow_run.html_url }}" \\\n'
        f"  -f description='Upstream CI failed; downstream {lane_label} not run'\n"
    )
    return {
        "if": "${{ github.event.workflow_run.conclusion != 'success' }}",
        "runs-on": SLIM_RUNNER,
        "steps": [
            _mint_step(),
            {
                "name": "Post downstream failure status",
                "env": {"GH_TOKEN": "${{ steps.mint.outputs.token }}"},
                "run": _BlockScalar(script),
            },
        ],
    }


def _report_result_job(lane: Execution, consumer_job_ids: Sequence[str]) -> dict[str, Any]:
    """Post the final commit status for `downstream/{lane}`: `failure` if any gated job
    failed or was cancelled, else `success`. `always()` so it still posts when a consumer
    fails, but still gated on CI success so a failed CI posts nothing at all.

    `validate` is included in `needs` so an orchestrator drift (validate failing, its
    consumers skipped) posts a failing status rather than a spurious success."""
    context = _status_context(lane)
    lane_label = "runner" if lane == EXECUTION_RUNNER else "HPC"
    needs = ["validate", *consumer_job_ids]
    results = " ".join(f"${{{{ needs.{jid}.result }}}}" for jid in needs)
    script = (
        "state=success\n"
        f"for r in {results}; do\n"
        '  if [ "$r" = failure ] || [ "$r" = cancelled ]; then\n'
        "    state=failure\n"
        "  fi\n"
        "done\n"
        "gh api -X POST \\\n"
        '  "/repos/${{ github.repository }}/statuses/${{ github.event.workflow_run.head_sha }}" \\\n'
        '  -f state="$state" \\\n'
        f"  -f context='{context}' \\\n"
        '  -f target_url="${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" \\\n'
        f'  -f description="Downstream {lane_label} tests $state"\n'
    )
    return {
        "needs": needs,
        "if": "${{ always() && github.event.workflow_run.conclusion == 'success' }}",
        "runs-on": SLIM_RUNNER,
        "steps": [
            _mint_step(),
            {
                "name": "Post final downstream status",
                "env": {"GH_TOKEN": "${{ steps.mint.outputs.token }}"},
                "run": _BlockScalar(script),
            },
        ],
    }


def _orchestrator_job_id(consumer_pkg: str) -> str:
    """Job ID for one orchestrator fan-out job — one per consumer package.

    A consumer reached by several originator kinds still gets a single caller
    job (its `from-jobs` carries the whole set), so the bare package id is
    unique and no kind suffix is needed.
    """
    return _id_segment(consumer_pkg)


def _edge_needs_dispatch(caller: Manifest, consumer: Manifest) -> bool:
    """True iff a public upstream fans out to a private consumer.

    That is the only edge that would leak: a reusable-workflow call runs the
    consumer's jobs inside the caller's (public) run, so a private consumer must
    be reached by cross-repo dispatch instead. All other combinations
    (public->public, private->private, private->public) keep the native
    reusable-workflow call — see render_orchestrator_workflow.
    """
    return caller.visibility == VISIBILITY_PUBLIC and consumer.visibility == VISIBILITY_PRIVATE


def _cross_package_deps(
    consumer_pkgs: Sequence[str], by_pkg: Mapping[str, Manifest], *, lane: Execution
) -> dict[str, set[str]]:
    """For each consumer package in scope, return the other consumer packages it depends on
    (collapsed across kinds) via cross-repo `needs`, restricted to `lane`.

    Only same-lane kinds contribute deps, so a consumer's hpc leg waits solely on
    upstream hpc packages and its runner leg solely on upstream runner packages —
    the two lanes never couple at a consumer boundary."""
    out: dict[str, set[str]] = {p: set() for p in consumer_pkgs}
    in_scope = set(consumer_pkgs)
    for cpkg in consumer_pkgs:
        cmanifest = by_pkg[cpkg]
        for mk in cmanifest.matrices.values():
            if mk.execution != lane:
                continue
            for need in mk.needs:
                pkg, _ = _split_need(need)
                if pkg is None or pkg == cpkg or pkg not in in_scope:
                    continue
                out[cpkg].add(pkg)
    return out


def _validate_job() -> dict[str, Any]:
    """Orchestrator's `validate` job: checks out the tested commit and runs the
    validate-generated-workflows composite to confirm this repo's manifest and its
    checked-in workflow YAMLs are in sync. Every consumer job prepends `validate` to
    its needs: so a drift fails fast — and skips the consumers, which then flips the
    posted status to failure via report-result.

    Under `on: workflow_run` a bare checkout would land on the default branch, so we
    pin `ref` to the tested head SHA. Gated on CI success like every other root job.
    """
    return {
        "if": _SUCCESS_GATE,
        "runs-on": SLIM_RUNNER,
        "steps": [
            _mint_step(),
            {
                "uses": "actions/checkout@v6",
                "with": {
                    "ref": "${{ github.event.workflow_run.head_sha }}",
                    "token": "${{ steps.mint.outputs.token }}",
                },
            },
            {
                "uses": "ecmwf/ci-infrastructure/actions/validate-generated-workflows@main",
                "with": {"token": "${{ steps.mint.outputs.token }}"},
            },
        ],
    }


def _from_jobs_input(from_jobs: Sequence[str]) -> str:
    """The `from-jobs` workflow input: a compact JSON array the consumer parses
    with `fromJSON(...)` and matches per-kind with `contains(...)`."""
    return json.dumps(sorted(from_jobs), separators=(",", ":"))


def _orchestrator_job(
    cpkg: str,
    crepo: str,
    cref: str,
    from_jobs: Sequence[str],
    dep_job_ids: Sequence[str],
    *,
    lane: Execution,
) -> dict[str, Any]:
    """Per-consumer caller job: invoke the consumer's cross-repo-trigger{-hpc}.yml
    (this lane's file) as a reusable workflow (workflow_call) and let GitHub wait for
    it natively. `secrets: inherit` flows the App + AWS secrets through, so the called
    workflow mints its own token internally (no per-consumer mint step here).

    `from-jobs` carries every in-lane originator kind reaching this consumer, so the one
    call wakes all its same-lane chains. `@<cref>` is a generation-time-static ref (from
    the manifest), so the `uses:` reference is a literal — GHA forbids expressions there.
    The tested commit's SHA/branch come from the `workflow_run` event; runtime branch
    matching still happens inside the called workflow via its `pick-ref` step.
    """
    cowner, cname = crepo.split("/", 1)
    suffix = _lane_suffix(lane)
    return {
        "name": cpkg,
        "needs": ["validate"] + list(dep_job_ids),
        "uses": f"{cowner}/{cname}/.github/workflows/cross-repo-trigger{suffix}.yml@{cref}",
        "with": {
            "from-repo": "${{ github.repository }}",
            "from-sha": "${{ github.event.workflow_run.head_sha }}",
            "from-jobs": _from_jobs_input(from_jobs),
            "branch": "${{ github.event.workflow_run.head_branch }}",
            "fallback-ref": cref,
        },
        "secrets": "inherit",
    }


def _orchestrator_dispatch_job(
    cpkg: str,
    crepo: str,
    cref: str,
    from_jobs: Sequence[str],
    dep_job_ids: Sequence[str],
    *,
    lane: Execution,
) -> dict[str, Any]:
    """Per-consumer dispatch job for a PRIVATE consumer of a PUBLIC upstream.

    Unlike _orchestrator_job (reusable-workflow call), this fires the consumer's
    cross-repo-trigger{-hpc}.yml via workflow_dispatch through dispatch-and-wait, so
    the consumer's run — and its logs — stay in the private repo. It then WAITS for
    that run's conclusion (polling run status, never logs), so this job turns
    red/green with the downstream and the cross-package `needs:` ordering is honoured.

    The hpc lane targets the `-hpc` workflow file via dispatch-and-wait's
    `workflow-file` input; the runner lane uses the action's default.
    """
    suffix = _lane_suffix(lane)
    dispatch_with: dict[str, Any] = {"consumer-repo": crepo}
    if lane == EXECUTION_HPC:
        dispatch_with["workflow-file"] = f"cross-repo-trigger{suffix}.yml"
    dispatch_with.update(
        {
            "ref": cref,
            "from-repo": "${{ github.repository }}",
            "from-sha": "${{ github.event.workflow_run.head_sha }}",
            "from-jobs": _from_jobs_input(from_jobs),
            "branch": "${{ github.event.workflow_run.head_branch }}",
            "fallback-ref": cref,
            "token": "${{ steps.mint.outputs.token }}",
            # No artifact to wait for (upstream consumes nothing back); gate on the
            # downstream run's conclusion instead so this job reflects its pass/fail.
            "artifact-names": "",
            "wait-for-run-conclusion": "true",
        }
    )
    return {
        "name": cpkg,
        "needs": ["validate"] + list(dep_job_ids),
        "runs-on": SLIM_RUNNER,
        "steps": [
            _mint_step(),
            {
                "name": f"Dispatch {cpkg} (private consumer)",
                "uses": "ecmwf/ci-infrastructure/actions/dispatch-and-wait@main",
                "with": dispatch_with,
            },
        ],
    }


def _check_orchestrator_caps(
    m: Manifest,
    self_closure: Mapping[str, dict[str, list[str]]],
    by_pkg: Mapping[str, Manifest],
    by_repo: Mapping[str, Manifest],
    *,
    lane: Execution,
) -> None:
    """Pre-validate one lane's orchestrator against two GHA limits: the
    256-job-per-run hard cap and the 20-distinct-reusable-workflows-per-top-level-caller
    cap. Each lane is its own top-level `workflow_run`, so the budgets are per lane and
    the counts here only ever shrink relative to the pre-split single run.

    The orchestrator emits ONE caller job per in-lane consumer package. Each call
    expands inside the same run to 1 resolve job plus the legs of every in-lane kind
    that accepts any originator reaching the consumer (its `from-jobs` carries them
    all). We sum these for the job cap, and count distinct consumer packages for the
    reusable-workflow cap. Base overhead is the orchestrator's own validate +
    report-start + report-result + report-ci-failure jobs.
    """
    # cpkg -> the in-lane originator (package, kind) pairs reaching it.
    origins: dict[str, set[tuple[str, str]]] = {}
    for orig_key, entry in self_closure.items():
        opkg, okind = orig_key.split("/", 1)
        if m.matrices[okind].execution != lane:
            continue
        for r in entry["consumers"]:
            origins.setdefault(by_repo[r].package_name, set()).add((opkg, okind))

    estimated_jobs = 4  # validate + report-start + report-result + report-ci-failure
    distinct_consumers = set(origins)
    for cpkg, orig_pairs in origins.items():
        cmanifest = by_pkg[cpkg]
        estimated_jobs += 2  # this consumer's caller job + its single resolve
        for ckind, mk in cmanifest.matrices.items():
            if TRIGGER_UPSTREAM_CHANGE not in mk.triggers or mk.execution != lane:
                continue
            refs = transitive_cross_repo_needs(cmanifest, ckind, by_pkg)
            if any((r.package, r.kind) in orig_pairs for r in refs):
                estimated_jobs += max(1, len(mk.legs))

    if estimated_jobs > ORCHESTRATOR_MAX_TOTAL_JOBS:
        raise SchemaError(
            f"{m.path}: orchestrator for {m.package_name} would expand to ~{estimated_jobs} jobs, "
            f"exceeding the safety limit of {ORCHESTRATOR_MAX_TOTAL_JOBS} (GHA hard cap is 256 "
            f"jobs per workflow run). Reduce matrix density or split via tiered fan-out."
        )

    # One `uses:` per consumer package == one distinct reusable workflow file.
    if len(distinct_consumers) > ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS:
        raise SchemaError(
            f"{m.path}: orchestrator for {m.package_name} would call {len(distinct_consumers)} distinct "
            f"reusable workflows, exceeding GHA's limit of {ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS} reusable "
            f"workflows callable from one top-level workflow run. Split the fan-out into tiers."
        )


def discover_manifests(root: Path) -> list[Path]:
    """Find every .ci/manifest.toml under sibling directories of `root`.

    `root` is expected to be the playground-downstream-CI directory (or any
    directory whose immediate children are repos).
    """
    return sorted(p for p in root.glob("*/.ci/manifest.toml") if p.is_file())


_FETCH_DEPTH_CAP: Final = 8  # Same depth budget resolve_deps uses for upstream BFS.


def _git(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run a `git` subcommand and return (rc, stdout_stripped). stderr is
    discarded — the caller only consults rc + stdout, and surfacing git's
    own diagnostics on top of our warning text just adds noise.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError) as e:
        return 1, str(e)
    return result.returncode, result.stdout.strip()


def _warn_if_local_drifts_from_remote(local: Manifest, local_path: Path, token: str | None) -> None:
    """Warn the user when their local working tree doesn't match what
    GitHub serves at `local.repo`'s default branch tip.

    Validation downstream walks a hybrid graph: the current repo from disk +
    all siblings from GitHub @HEAD. If the user has un-pushed local commits
    (or a dirty .ci/manifest.toml), cross-repo errors look like sibling-side
    bugs but are really "your local view is stale". This surfaces the gap
    before the BFS runs.

    Three outcomes:
      - manifest dirty in working tree -> dirty-tree warning
      - clean tree, local HEAD != remote default-branch SHA -> sha-mismatch
      - aligned -> silent

    Any probe failure (git missing, not a git checkout, GraphQL down, no
    auth) emits a single soft warning and returns. Never raises; never
    changes the script's exit code.
    """
    repo_dir = local_path.resolve().parent.parent  # .ci/manifest.toml -> repo root

    rc_head, local_head = _git(["rev-parse", "HEAD"], cwd=repo_dir)
    if rc_head != 0 or not local_head:
        print(
            f"::warning::could not verify local-vs-remote drift "
            f"(git rev-parse HEAD failed in {repo_dir}); cross-repo validation may run on a hybrid view.",
            file=sys.stderr,
        )
        return

    rc_status, status_out = _git(["status", "--porcelain"], cwd=repo_dir)
    if rc_status != 0:
        print(
            f"::warning::could not verify local-vs-remote drift "
            f"(git status failed in {repo_dir}); cross-repo validation may run on a hybrid view.",
            file=sys.stderr,
        )
        return

    remote = fetch_repo_head(local.repo, token)
    if remote is None:
        print(
            f"::warning::could not verify local-vs-remote drift "
            f"(GitHub lookup for {local.repo} returned no default-branch SHA); "
            f"cross-repo validation may run on a hybrid view.",
            file=sys.stderr,
        )
        return

    default_branch, remote_head = remote

    if status_out:
        print(
            f"::warning::repository has uncommitted local changes; "
            f"cross-repo validation will use the working-tree version "
            f"(siblings are validated against {local.repo}@{default_branch}). "
            f"Commit and push to align.",
            file=sys.stderr,
        )
        return

    if local_head != remote_head:
        print(
            f"::warning::local HEAD {local_head[:7]} differs from "
            f"{local.repo}@{default_branch} {remote_head[:7]}; "
            f"un-pushed commits aren't visible to sibling validators. Push to align.",
            file=sys.stderr,
        )


def _fetch_sibling_manifests(local: Manifest, token: str | None, manifest_path: str) -> list[Manifest]:
    """BFS the dep+trigger graph outward from `local`, fetching each sibling's
    manifest TOML text via GraphQL and parsing it. Missing manifests (None text)
    are skipped — `validate_graph` already treats unknown repos as external, so
    a sibling that lacks a manifest at HEAD just doesn't contribute to validation
    rather than failing the whole check.

    The resulting list always includes `local` first; siblings in arbitrary order.
    """
    seen: dict[str, Manifest] = {local.repo: local}
    queue: list[tuple[str, str]] = []
    for d in local.deps:
        queue.append((d.repo, "HEAD"))
    for t in local.triggers:
        queue.append((t.repo, t.ref or "HEAD"))

    for _ in range(_FETCH_DEPTH_CAP):
        layer = [(r, ref) for (r, ref) in queue if r not in seen]
        if not layer:
            break
        results = fetch_manifests_layer(layer, sync_branch=None, token=token, manifest_path=manifest_path)
        next_q: list[tuple[str, str]] = []
        for (repo, ref), (text, _sync) in results.items():
            if text is None:
                continue
            m = parse_manifest_text(text, Path(f"github://{repo}@{ref}/{manifest_path}"))
            seen[repo] = m
            for d in m.deps:
                next_q.append((d.repo, "HEAD"))
            for t in m.triggers:
                next_q.append((t.repo, t.ref or "HEAD"))
        queue = next_q

    # Stable order: local first, siblings sorted by repo name for predictable error messages.
    return [local] + sorted((m for m in seen.values() if m.repo != local.repo), key=lambda m: m.repo)


def _write_or_check_path(out: Path, content: str | None, check: bool) -> bool:
    """Reconcile a single file with its desired content.

    Shared by write_or_check (.github/workflows/cross-repo-trigger.yml) and
    write_orchestrator (.github/workflows/trigger-downstream.yml).
    `content=None` means "this file should not exist"; if it does we delete it
    (or report stale).

    Returns True iff a change was needed.
    """
    existing = out.read_text() if out.exists() else None
    if content is None:
        if existing is None:
            return False
        if not check:
            out.unlink()
        return True
    if existing == content:
        return False
    if not check:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
    return True


def write_or_check(repo_root: Path, content: str | None, check: bool, *, lane: Execution = EXECUTION_RUNNER) -> bool:
    """Write `.github/workflows/cross-repo-trigger{-hpc}.yml` for one repo/lane, and
    (runner lane only) remove the legacy `triggered-by-upstream.yml` from before the
    rename.

    When `content` is None (no kind in the manifest opts into cross-repo dispatch on
    this lane) we delete any existing file rather than leaving it alone: the manifest
    is the source of truth, and a stale workflow pointing at non-existent kinds would
    be a foot-gun.
    """
    if lane == EXECUTION_RUNNER:
        legacy = repo_root / ".github" / "workflows" / "triggered-by-upstream.yml"
        if legacy.exists() and not check:
            legacy.unlink()
    suffix = _lane_suffix(lane)
    return _write_or_check_path(repo_root / ".github" / "workflows" / f"cross-repo-trigger{suffix}.yml", content, check)


def write_orchestrator(
    repo_root: Path, content: str | None, check: bool, *, lane: Execution = EXECUTION_RUNNER
) -> bool:
    """Write `.github/workflows/trigger-downstream{-hpc}.yml` for one repo/lane.
    See _write_or_check_path."""
    suffix = _lane_suffix(lane)
    return _write_or_check_path(repo_root / ".github" / "workflows" / f"trigger-downstream{suffix}.yml", content, check)


@click.command(help=__doc__)
@click.option(
    "--manifest-path",
    "manifest_path",
    default=".ci/manifest.toml",
    help="Path to this repo's manifest (default: .ci/manifest.toml). "
    "Also the path used when fetching sibling manifests over GraphQL.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Don't write; exit 1 if any generated file is stale.",
)
def main(manifest_path: str, check: bool) -> None:
    _run(manifest_path, check)


def _run(manifest_path: str, check: bool) -> None:
    local_manifest_path = Path(manifest_path)
    if not local_manifest_path.is_file():
        raise CIError(f"no manifest found at {local_manifest_path}")

    try:
        local = parse_manifest(local_manifest_path)
    except SchemaError as e:
        raise CIError(str(e)) from e

    # select_token() is None when no env-var token is set; that's fine — gh
    # falls back to GITHUB_TOKEN / keychain auth, and a real auth failure
    # surfaces from `gh api graphql` directly rather than being second-guessed.
    token = select_token()

    _warn_if_local_drifts_from_remote(local, local_manifest_path, token)

    manifests = _fetch_sibling_manifests(local, token, manifest_path)

    try:
        validate_graph(manifests)
    except SchemaError as e:
        raise CIError(str(e)) from e

    closures = compute_transitive_consumers(manifests)
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    try:
        # Only render+diff the local manifest; sibling manifests existed solely
        # to feed validate_graph and compute_transitive_consumers.
        changed = _render_one_repo(local, by_pkg, by_repo, closures, check)
    except SchemaError as e:
        raise CIError(str(e)) from e

    _report(changed, check, manifest_path)


def _render_one_repo(
    m: Manifest,
    by_pkg: Mapping[str, Manifest],
    by_repo: Mapping[str, Manifest],
    closures: Mapping[str, dict[str, dict[str, list[str]]]],
    check: bool,
) -> list[tuple[Literal["delete", "update", "create"], Path]]:
    changed: list[tuple[Literal["delete", "update", "create"], Path]] = []
    wf_dir = m.repo_root / ".github" / "workflows"

    # Two lanes, two files per side. A lane with no runnable/consumer content renders
    # None, which deletes any stale file for that lane (e.g. a runner-only leaf never
    # gets a -hpc file).
    for lane in (EXECUTION_RUNNER, EXECUTION_HPC):
        suffix = _lane_suffix(lane)

        wf_content = render_workflow(m, by_pkg, lane=lane)
        wf_path = wf_dir / f"cross-repo-trigger{suffix}.yml"
        wf_existed = wf_path.exists()
        if write_or_check(m.repo_root, wf_content, check, lane=lane):
            if wf_content is None:
                changed.append(("delete", wf_path))
            else:
                changed.append(("update" if wf_existed else "create", wf_path))

        orch_content = render_orchestrator_workflow(m, by_pkg, by_repo, closures, lane=lane)
        orch_path = wf_dir / f"trigger-downstream{suffix}.yml"
        orch_existed = orch_path.exists()
        if write_orchestrator(m.repo_root, orch_content, check, lane=lane):
            if orch_content is None:
                changed.append(("delete", orch_path))
            elif orch_existed:
                changed.append(("update", orch_path))
            else:
                changed.append(("create", orch_path))

    # Surface the legacy-file cleanup so --check still flags pre-rename repos
    # as stale (otherwise CI would pass even when the old file is still present).
    legacy_path = wf_dir / "triggered-by-upstream.yml"
    if check and legacy_path.exists():
        changed.append(("delete", legacy_path))

    return changed


def _regen_command(manifest_path: str) -> str:
    """Reconstruct the invocation that would *write* the regenerated YAML
    instead of checking it. We rebuild from parsed args (not sys.argv) so
    the suggestion is canonicalised regardless of how the user invoked it.
    """
    parts: list[str] = ["python3", "generate_downstream_ci.py"]
    if manifest_path != ".ci/manifest.toml":
        parts += ["--manifest-path", manifest_path]
    return shlex.join(parts)


def _report(
    changed: Sequence[tuple[Literal["delete", "update", "create"], Path]],
    check: bool,
    manifest_path: str,
) -> None:
    if check and changed:
        stale = "\n".join(f"  - [{action}] {p}" for action, p in changed)
        # gh keychain auth is a prerequisite when GH_TOKEN isn't set.
        # `gh auth login` is idempotent if you're already logged in, so the
        # hint is harmless either way. Only the first line carries the ::error::
        # annotation; GitHub renders the rest as plain log.
        raise CIError(
            "generated files are out of date:\n"
            f"{stale}\n"
            "\n"
            "To regenerate, run from this repo (drops --check, writes in place):\n"
            "\n"
            "  gh auth login        # only needed if 'gh' isn't already authenticated\n"
            f"  {_regen_command(manifest_path)}\n"
            "\n"
            "Where generate_downstream_ci.py comes from https://github.com/ecmwf/ci-infrastructure.\n"
            "Then commit the regenerated files alongside your manifest changes."
        )
    for action, p in changed:
        print(f"{action} {p}")


if __name__ == "__main__":
    main()
