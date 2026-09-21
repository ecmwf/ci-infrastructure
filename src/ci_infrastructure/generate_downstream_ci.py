#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Validate the cross-repo trigger / needs graph of every consumer repo's
.ci/manifest.toml and emit its generated workflows, one pair per lane
(runner, `-hpc`):

  - cross-repo-trigger{-hpc}.yml: per-kind jobs, entered via `workflow_call`
    from an upstream orchestrator (`from-jobs`) or via `workflow_dispatch` from
    a consumer's recovery path (`rebuild-request`).

  - trigger-downstream{-hpc}.yml (repos with consumers only): on a completed
    CI run, fan out one job per transitive consumer package and post a
    `downstream/<lane>` commit status. A public->private edge is dispatched
    rather than called (see `_edge_needs_dispatch`).

Manifest schema: the `_*Raw` pydantic models plus the `_check_*` graph
invariants; a violation fails with exit 1 in TOML notation (`[matrix.build]`).

Usage:

    cd <consumer-repo>
    ci-infrastructure-generate [--check [--fail-on-drift]]

Sibling manifests named in [[deps]] / [[trigger-downstream]] are fetched over
the GitHub GraphQL API (GH_TOKEN, or gh's keychain auth). For a coordinated
change still on branches, read local clones instead:

    ci-infrastructure-generate --sibling-root ~/code/rollout

Without --check, the YAML is written in place. --check writes nothing and
reports stale files as a warning (a failure under --fail-on-drift). It compares
PARSED documents, so hand-added comments never count as drift.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import tomllib
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, TypeVar

import click
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ._errors import CIError
from ._github_api import (
    EXECUTION_HPC,
    EXECUTION_RUNNER,
    Execution,
    ManifestSchemaError,
    fetch_manifests_layer,
    lane_suffix,
    resolve_reuse_matrix,
    select_token,
)
from .hpc import jobscript

GENERATED_HEADER: Final = (
    "# GENERATED FILE - DO NOT EDIT.\n"
    "# Regenerate via the ci-infrastructure-generate CLI.\n"
    "# Source of truth: each repo's .ci/manifest.toml.\n"
)


def _normalise_header(header: str) -> str:
    """Strip [generated].header and follow it with one blank line."""
    stripped = header.strip("\n")
    return f"{stripped}\n\n" if stripped else ""


# 1-CPU, unprivileged, cheap: only for jobs that talk to APIs and shuffle YAML.
SLIM_RUNNER: Final = "ubuntu-slim"


Step: TypeAlias = dict[str, Any]
NodeT = TypeVar("NodeT")


class _BlockScalar(str):
    """Dumped as a `|` block scalar."""


class _WorkflowDumper(yaml.SafeDumper):
    """YAML 1.2 booleans, so `on:` is not quoted."""


class _WorkflowLoader(yaml.SafeLoader):
    """YAML 1.2 booleans, so `on:` does not load as True."""


def _use_yaml12_bools(cls: type[yaml.SafeDumper] | type[yaml.SafeLoader]) -> None:
    cls.yaml_implicit_resolvers = {
        k: [(tag, regexp) for tag, regexp in v if tag != "tag:yaml.org,2002:bool"]
        for k, v in cls.yaml_implicit_resolvers.items()
    }
    # Both codes: whether this call is typed depends on the unpinned types-PyYAML.
    cls.add_implicit_resolver(  # type: ignore[no-untyped-call, unused-ignore]
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )


_use_yaml12_bools(_WorkflowDumper)
_use_yaml12_bools(_WorkflowLoader)


def _parse_workflow(text: str) -> Any:
    return yaml.load(text, Loader=_WorkflowLoader)


def _block_scalar_representer(dumper: _WorkflowDumper, data: _BlockScalar) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


_WorkflowDumper.add_representer(_BlockScalar, _block_scalar_representer)


def _dump_workflow(doc: Mapping[str, Any]) -> str:
    """Dump in insertion order, never folding long expressions."""
    out: str = yaml.dump(
        dict(doc),
        Dumper=_WorkflowDumper,
        sort_keys=False,
        default_flow_style=False,
        width=10**9,
        allow_unicode=True,
    )
    return out


# [matrix.<kind>].triggers; an empty set keeps the kind out of cross-repo-trigger.yml.
TRIGGER_UPSTREAM_CHANGE: Final = "upstream-change"
TRIGGER_REBUILD_REQUEST: Final = "rebuild-request"
_VALID_TRIGGERS: Final = frozenset({TRIGGER_UPSTREAM_CHANGE, TRIGGER_REBUILD_REQUEST})

Visibility: TypeAlias = Literal["public", "private"]
VISIBILITY_PUBLIC: Final[Visibility] = "public"
VISIBILITY_PRIVATE: Final[Visibility] = "private"


def _lane_label(lane: Execution) -> str:
    return "runner" if lane == EXECUTION_RUNNER else "HPC"


def _status_context(lane: Execution) -> str:
    """Per lane, so each can be a separate required check."""
    return f"downstream/{lane}"


class SchemaError(Exception):
    """A manifest violates the schema or a cross-repo invariant."""


@dataclass(frozen=True)
class MatrixKind:
    """One [matrix.<kind>] table plus its [[matrix.<kind>.include]] legs."""

    name: str
    triggers: frozenset[str]
    needs: Sequence[str]
    reuse_matrix: str | None
    legs: Sequence[dict[str, Any]]  # after reuse-matrix resolution
    execution: Execution
    action: str  # runner kinds: the composite the per-kind job calls
    job_script: str | None  # hpc kinds: the recipe submitted to SLURM
    forwarded_inputs: tuple[str, ...]  # leg fields passed to the action's `with:`
    forwarded_deps_outputs: tuple[str, ...]  # fetch-deps outputs passed to the action's `with:`
    # False for test kinds: no publish step; the action gets `own-artifact-name` instead.
    publishes: bool
    container_credentials: bool
    ctest: bool  # run ctest on the build tree before publishing (runner only)
    ctest_args: str


@dataclass(frozen=True)
class TriggerDownstream:
    repo: str
    ref: str


@dataclass(frozen=True)
class DepRef:
    repo: str
    package: str


@dataclass
class Manifest:
    """Parsed view of one repo's .ci/manifest.toml."""

    path: Path
    repo_root: Path
    package_name: str
    repo: str
    compiler_inputs: tuple[str, ...] = ()
    visibility: Visibility = VISIBILITY_PRIVATE
    # [package].submodules: forwarded to actions/checkout on the build jobs.
    submodules: str | None = None
    deps: list[DepRef] = field(default_factory=list)
    triggers: list[TriggerDownstream] = field(default_factory=list)
    matrices: dict[str, MatrixKind] = field(default_factory=dict)
    # [downstream-gate].label: a PR fans out only if it carries this label; a push always does.
    downstream_gate_label: str | None = None
    # [generated].header: comment lines above GENERATED_HEADER in every generated file.
    generated_header: str = ""


_RESERVED_MATRIX_KEYS: Final = frozenset(
    {
        "include",
        "defaults",
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
        "container-credentials",
        "ctest",
        "ctest-args",
    }
)
# Outputs of actions/fetch-deps.
_VALID_DEPS_OUTPUTS: Final = frozenset({"cmake-prefix-path"})
_ACTION_PATH_RE: Final = re.compile(r"^\./\.github/actions/[A-Za-z0-9_-]+$")
_ARTIFACT_PREFIX_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


class _PackageRaw(BaseModel):
    # resolve_deps owns the full [package] schema.
    model_config = ConfigDict(extra="ignore")
    name: str
    repo: str
    compiler_inputs: tuple[str, ...] = Field(default=(), alias="compiler-inputs")
    # Fail closed: an unlabelled repo is private.
    visibility: Visibility = VISIBILITY_PRIVATE
    submodules: Literal["true", "recursive"] | None = None


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
    defaults: dict[str, Any] = Field(default_factory=dict)
    execution: Execution = EXECUTION_RUNNER
    action: str = ""
    job_script: str = Field(default="", alias="job-script")
    forwarded_inputs: tuple[str, ...] = Field(default=(), alias="forwarded-inputs")
    forwarded_deps_outputs: tuple[str, ...] = Field(default=(), alias="forwarded-deps-outputs")
    publishes: bool = True
    artifact_prefix: str | None = Field(default=None, alias="artifact-prefix")
    container_credentials: bool = Field(default=False, alias="container-credentials")
    ctest: bool = False
    ctest_args: str = Field(default="", alias="ctest-args")

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
        if len(set(v)) != len(v):
            raise ValueError(f"triggers must not contain duplicates: {list(v)}")
        return v

    @field_validator("action")
    @classmethod
    def _action_path_shape(cls, v: str) -> str:
        # Required-when-triggered is checked in _resolve_matrices.
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


class _GeneratedRaw(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header: str = ""

    @field_validator("header")
    @classmethod
    def _comment_lines_only(cls, v: str) -> str:
        for line in v.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                raise ValueError(
                    f"header lines must be YAML comments starting with '#'; got {line!r}. "
                    "Anything else would land above the workflow document and corrupt it."
                )
        return v


class _DownstreamGateRaw(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1)


class _ManifestRaw(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    package: _PackageRaw
    deps: tuple[_DepRefRaw, ...] = ()
    trigger_downstream: tuple[_TriggerDownstreamRaw, ...] = Field(default=(), alias="trigger-downstream")
    downstream_gate: _DownstreamGateRaw | None = Field(default=None, alias="downstream-gate")
    generated: _GeneratedRaw | None = None
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
    """The first pydantic error as one line in TOML notation."""
    err = exc.errors()[0]
    loc: tuple[str | int, ...] = tuple(err["loc"])
    # pydantic prefixes errors raised from our validators.
    msg = err["msg"].removeprefix("Value error, ")
    prefix = _format_loc(loc)
    sep = " " if prefix and not prefix.endswith(" ") else ""
    return f"{path}: {prefix}{sep}{msg}".rstrip()


#: How a pydantic loc head renders in TOML notation. `array` heads are arrays of
#: tables, so a numeric index becomes `[[deps]][2]`; `subtable` absorbs the next
#: element into the table name (`[matrix.build]`); `table` is a plain table.
_LOC_HEADS: Final = {
    "trigger-downstream": "array",
    "trigger_downstream": "array",
    "deps": "array",
    "matrix": "subtable",
    "package": "table",
    "generated": "table",
}


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
    head, rest = str(loc[0]), loc[1:]
    shape = _LOC_HEADS.get(head)
    if shape is None:
        return ".".join(str(p) for p in loc)
    name = head.replace("_", "-")  # the field is trigger_downstream, the key is trigger-downstream
    if shape == "array":
        prefix = f"[[{name}]]"
        if rest and isinstance(rest[0], int):
            prefix, rest = f"{prefix}[{rest[0]}]", rest[1:]
    elif shape == "subtable" and rest:
        prefix, rest = f"[{name}.{rest[0]}]", rest[1:]
    else:
        prefix = f"[{name}]"
    if rest:
        prefix += "." + ".".join(str(p) for p in rest)
    return prefix


def parse_manifest(path: Path) -> Manifest:
    """Parse only what the generator needs; resolve_deps owns the rest."""
    with path.open("rb") as fh:
        raw_dict = tomllib.load(fh)
    return _build_manifest(path, raw_dict)


def parse_manifest_text(text: str, path: Path) -> Manifest:
    """Parse manifest TOML text; `path` is synthetic and used only in error messages."""
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
        compiler_inputs=raw.package.compiler_inputs,
        visibility=raw.package.visibility,
        submodules=raw.package.submodules,
        deps=[DepRef(repo=d.repo, package=d.package) for d in raw.deps],
        triggers=[TriggerDownstream(repo=t.repo, ref=t.ref) for t in raw.trigger_downstream],
        matrices=matrices,
        downstream_gate_label=raw.downstream_gate.label if raw.downstream_gate else None,
        generated_header=_normalise_header(raw.generated.header if raw.generated else ""),
    )


def _resolve_matrices(path: Path, raw_matrix: Mapping[str, _MatrixKindRaw]) -> dict[str, MatrixKind]:
    """Resolve reuse-matrix into legs and apply the cross-field rules."""
    blocks = {
        k: {"reuse-matrix": b.reuse_matrix, "include": b.include, "defaults": b.defaults} for k, b in raw_matrix.items()
    }

    resolved: dict[str, MatrixKind] = {}
    for kind, body in raw_matrix.items():
        try:
            legs = resolve_reuse_matrix(kind, body.include, body.reuse_matrix, blocks)
        except ManifestSchemaError as e:
            raise SchemaError(f"{path}: {e}") from e

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
            if body.ctest:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] sets `ctest` with execution = 'hpc'; "
                    f"run ctest inside {body.job_script or 'the job-script'} instead, "
                    "where it executes on the compute node"
                )
        else:
            if body.job_script:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] sets `job-script` but execution is 'runner'; "
                    "`job-script` only applies to execution = 'hpc'"
                )
            if body.triggers and not body.action:
                raise SchemaError(
                    f"{path}: [matrix.{kind}] declares triggers = {sorted(body.triggers)!r} "
                    "but has no `action`; cross-repo-trigger.yml has nothing to invoke"
                )

        if body.ctest_args and not body.ctest:
            raise SchemaError(
                f"{path}: [matrix.{kind}] sets `ctest-args` without `ctest = true`; the arguments would never be used"
            )

        if body.ctest and not body.publishes:
            raise SchemaError(
                f"{path}: [matrix.{kind}] sets `ctest` with `publishes = false`; "
                "a non-publishing kind has no build tree — run the tests inside "
                f"{body.action or 'its action'} instead"
            )

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
            container_credentials=body.container_credentials,
            ctest=body.ctest,
            ctest_args=body.ctest_args.strip(),
        )
    return resolved


@dataclass(frozen=True)
class JobRef:
    package: str
    kind: str


def _split_need(raw: str) -> tuple[str | None, str]:
    """Return (package, kind) for cross-repo refs, (None, kind) for local ones."""
    if "/" in raw:
        pkg, _, kind = raw.partition("/")
        return pkg, kind
    return None, raw


def _index_unique(manifests: Sequence[Manifest], key: Callable[[Manifest], str], label: str) -> dict[str, Manifest]:
    """Index manifests by `key`; a collision is a SchemaError."""
    out: dict[str, Manifest] = {}
    for m in manifests:
        if key(m) in out:
            raise SchemaError(f"duplicate {label} {key(m)!r}: {out[key(m)].path} and {m.path}")
        out[key(m)] = m
    return out


def validate_graph(manifests: Sequence[Manifest]) -> None:
    """Run every cross-repo invariant. Raises SchemaError on the first violation."""
    by_repo = _index_unique(manifests, lambda m: m.repo, "manifest for repo")
    by_pkg = _index_unique(manifests, lambda m: m.package_name, "package name")

    _check_subset_invariant(manifests, by_repo)
    _check_trigger_cycles(manifests, by_repo)
    _check_needs(manifests, by_pkg, by_repo)
    _check_kinds_have_legs(manifests)
    _check_leg_identity_uniqueness(manifests)


# Leg fields make_artifact_name reads (plus [package].compiler-inputs).
_FIXED_NAME_FIELDS: Final = ("build-type", "platform", "python-version", "options")


def _artifact_identity(leg: Mapping[str, Any], compiler_inputs: Sequence[str]) -> tuple[str, ...]:
    """The projection of a leg the artifact name depends on, with resolve_deps' defaults. Never raises."""
    parts = [str(leg.get(f, "Release" if f == "build-type" else "")).strip() for f in _FIXED_NAME_FIELDS]
    parts.extend(str(leg.get(f, "")).strip() for f in sorted(compiler_inputs))
    return tuple(parts)


def _check_leg_identity_uniqueness(manifests: Sequence[Manifest]) -> None:
    """No two legs of a publishing kind may share an artifact identity."""
    for m in manifests:
        for kind, mk in m.matrices.items():
            if not mk.publishes:
                continue
            seen: dict[tuple[str, ...], dict[str, Any]] = {}
            for leg in mk.legs:
                identity = _artifact_identity(leg, m.compiler_inputs)
                twin = seen.get(identity)
                if twin is not None:
                    shared = dict(zip((*_FIXED_NAME_FIELDS, *sorted(m.compiler_inputs)), identity))
                    differ = sorted(k for k in set(leg) | set(twin) if str(leg.get(k)) != str(twin.get(k)))
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}] has two legs with the same artifact identity "
                        f"{shared} — they differ only in {differ or 'nothing'}, which is not part of "
                        f"the artifact name, so they would publish different builds under one name. "
                        f"The name is built from platform + "
                        f"{sorted(m.compiler_inputs) or '(no compiler fields)'} + build-type + "
                        f"python-version + options; give them distinct values there. For a different "
                        f"module set or compiler that means a distinct `platform` slug (e.g. "
                        f"'hpc-atos-gnu-r2'), which is also what invalidates the cache. Or drop one."
                    )
                seen[identity] = leg


def validate_job_templates(m: Manifest) -> None:
    """Check each .j2 job-script exists, parses, and reads only names its legs declare.

    Pass the local manifest only: siblings have no recipes on disk.
    """
    if not m.path.is_file():
        return
    for kind, mk in m.matrices.items():
        if mk.execution != EXECUTION_HPC:
            continue
        for leg in mk.legs:
            spec = str(leg.get("job-script") or mk.job_script or "")
            if not jobscript.is_job_template(spec):
                continue
            path = m.repo_root / spec.removeprefix("./")
            if not path.is_file():
                raise SchemaError(f"{m.path}: [matrix.{kind}] job-script '{spec}' does not exist at {path}")
            try:
                missing = jobscript.undeclared_template_names(
                    path.read_text(), leg, template_name=str(path), search_path=path.parent
                )
            except jobscript.JobTemplateError as exc:
                raise SchemaError(f"{m.path}: [matrix.{kind}] {exc}") from exc
            if missing:
                declared = sorted(k for k in leg if k != "_resolved")
                raise SchemaError(
                    f"{m.path}: [matrix.{kind}] recipe '{spec}' reads {sorted(missing)}, which this "
                    f"leg does not declare. The leg has {declared}; a template may read those "
                    f"(hyphens as underscores), plus `leg`, `artifact_name` and the defaults "
                    f"{sorted(jobscript.JOB_TEMPLATE_DEFAULTS)}. Add the key to the "
                    f"leg, or drop it from the recipe — they are meant to say the same thing."
                )


def _check_subset_invariant(manifests: Sequence[Manifest], by_repo: Mapping[str, Manifest]) -> None:
    """Triggers are a subset of reverse-deps: if A triggers B, then B must list A as a dep."""
    for m in manifests:
        for t in m.triggers:
            target = by_repo.get(t.repo)
            if target is None:
                continue
            depends_on_us = any(d.repo == m.repo for d in target.deps)
            if not depends_on_us:
                raise SchemaError(
                    f"{m.path}: [[trigger-downstream]] points at {t.repo}, but "
                    f"{target.path} does not list {m.repo} as a [[deps]] entry "
                    f"(triggers must be a subset of the reverse-deps graph)"
                )


def _require_acyclic(
    graph: Mapping[NodeT, Sequence[NodeT]], roots: Iterable[NodeT], *, label: str, render: Callable[[NodeT], str]
) -> None:
    """Three-colour DFS; raises SchemaError naming the cycle."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[NodeT, int] = defaultdict(lambda: WHITE)

    def dfs(node: NodeT, stack: list[NodeT]) -> None:
        color[node] = GRAY
        for nxt in graph.get(node, ()):
            if color[nxt] == GRAY:
                cycle = stack[stack.index(nxt) :] + [nxt]
                raise SchemaError(f"{label}: " + " -> ".join(render(n) for n in cycle))
            if color[nxt] == WHITE:
                dfs(nxt, stack + [nxt])
        color[node] = BLACK

    for root in roots:
        if color[root] == WHITE:
            dfs(root, [root])


def _check_trigger_cycles(manifests: Sequence[Manifest], by_repo: Mapping[str, Manifest]) -> None:
    """The [[trigger-downstream]] graph must be acyclic; external targets are dropped."""
    graph = {m.repo: [t.repo for t in m.triggers if t.repo in by_repo] for m in manifests}
    _require_acyclic(graph, [m.repo for m in manifests], label="trigger-downstream cycle", render=str)


def _check_needs(
    manifests: Sequence[Manifest],
    by_pkg: Mapping[str, Manifest],
    by_repo: Mapping[str, Manifest],
) -> None:
    """Resolve every entry in matrix.needs and verify cross-repo fan-out reachability."""
    cross_edges: list[tuple[Manifest, str, JobRef]] = []

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
                    raise SchemaError(
                        f"{m.path}: [matrix.{kind}].needs references {pkg}/{target_kind}, "
                        f"but [matrix.{target_kind}] in {upstream.path} has no triggers — "
                        f"the kind exists for push/PR runs only and can't originate a "
                        f"cross-repo dispatch"
                    )
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
    """Cross-repo needs between (package, kind) nodes must be acyclic."""
    out_edges: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for downstream_m, downstream_kind, upstream_ref in cross_edges:
        out_edges[(downstream_m.package_name, downstream_kind)].append((upstream_ref.package, upstream_ref.kind))
    _require_acyclic(
        out_edges,
        list(out_edges),
        label="cross-repo needs cycle",
        render=lambda node: f"{node[0]}/{node[1]}",
    )


def _check_kinds_have_legs(manifests: Sequence[Manifest]) -> None:
    """A triggered or reuse-matrix kind needs at least one leg."""
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
    """Every cross-repo (package, kind) this kind transitively needs; its from-jobs filter set."""
    cross: list[JobRef] = []
    seen_refs: set[tuple[str, str]] = set()
    seen_local: set[tuple[str, str]] = set()

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
    """cross-repo-trigger{-hpc}.yml for the kinds with triggers on `lane`, or None."""
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
        # dispatch-id lets resolve_deps find the run it dispatched; ignored under workflow_call.
        "run-name": ("Cross-repo trigger (${{ inputs.from-repo }}@${{ inputs.from-sha }}) [${{ inputs.dispatch-id }}]"),
        "on": {
            "workflow_call": {"inputs": _shared_trigger_inputs()},
            "workflow_dispatch": {"inputs": _workflow_dispatch_inputs()},
        },
        # A static package token, not github.workflow: under workflow_call that is
        # the caller's, and sharing its group deadlocks. No cancel, so a late
        # dispatcher reuses the in-flight run.
        "concurrency": {
            "group": f"cross-repo-trigger-{lane}-{m.package_name}-" + "${{ github.ref }}",
            "cancel-in-progress": False,
        },
        # setup-sccache has no fallback for SCCACHE_BUCKET: artifacts and sccache
        # live in separate buckets.
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
    return m.generated_header + GENERATED_HEADER + _dump_workflow(doc)


def _shared_trigger_inputs() -> dict[str, dict[str, Any]]:
    """Inputs shared by workflow_call and workflow_dispatch."""
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


def _workflow_dispatch_inputs() -> dict[str, dict[str, Any]]:
    """The shared inputs plus `dispatch-id`."""
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
    """OR of `contains(from-jobs, 'pkg/kind')` per ref and/or `inputs.rebuild-request`; "false" if none."""
    triggers_set = frozenset(triggers)
    clauses: list[str] = []
    if TRIGGER_UPSTREAM_CHANGE in triggers_set:
        clauses.extend(f"contains(fromJSON(inputs.from-jobs), '{r.package}/{r.kind}')" for r in refs)
    if TRIGGER_REBUILD_REQUEST in triggers_set:
        clauses.append("inputs.rebuild-request")
    return " || ".join(clauses) if clauses else "false"


def _mint_step() -> Step:
    """App token, minted per job: masked values do not survive job outputs."""
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
                # Explicit repository: under workflow_call github.repository is the caller's.
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
                    # Enables dispatching a producer whose artifact is missing.
                    "client-id": "${{ secrets.CI_PERMISSIONS_APP_CLIENT_ID }}",
                    "app-private-key": "${{ secrets.CI_PERMISSIONS_APP_PRIVATE_KEY }}",
                },
            },
        ],
    }


def _decode_step(mk: MatrixKind) -> Step:
    """Decode forwarded leg fields, deps and own-artifact-name into step outputs."""
    # A field missing from some leg is optional and may be empty.
    optional_fields = {fld for fld in mk.forwarded_inputs if any(fld not in leg for leg in mk.legs)}
    var_assignments: list[str] = []
    for fld in mk.forwarded_inputs:
        var = jobscript.template_var(fld)
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
        output_lines.append(f'  echo "{fld}=${{{jobscript.template_var(fld)}}}"')
    output_lines.append('  echo "deps-json=${deps_json}"')
    output_lines.append('  echo "own-artifact-name=${own_artifact_name}"')

    body_lines = [
        "set -euo pipefail",
        "shopt -s inherit_errexit 2>/dev/null || true",
        "command -v jq >/dev/null || {",
        '  echo "::error::jq is required but not installed in this runner/image." >&2',
        "  exit 1",
        "}",
        # Via env, never a `${{ }}` in the script: a quote in the leg would become shell.
        'leg="$MATRIX_LEG"',
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
        "env": {"MATRIX_LEG": "${{ toJSON(matrix) }}"},
        "run": _BlockScalar("\n".join(body_lines) + "\n"),
    }


def _setup_python_step(mk: MatrixKind) -> Step | None:
    """setup-python before Fetch, whose needs-python wheel install needs the leg's interpreter."""
    if not any("python-version" in leg for leg in mk.legs):
        return None
    return {
        "name": "Set up Python ${{ steps.m.outputs.python-version }}",
        "uses": "actions/setup-python@v6",
        "with": {"python-version": "${{ steps.m.outputs.python-version }}"},
    }


def _action_call_step(mk: MatrixKind) -> Step:
    """Call the kind's composite with forwarded inputs."""
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
    """build-on-hpc step: a drop-in for _action_call_step that also publishes (unless publishes=false)."""
    assert mk.job_script is not None
    with_block: dict[str, str] = {
        "site": "${{ matrix.site }}",
        "job-script": f"${{{{ matrix.job-script || '{mk.job_script}' }}}}",
        "matrix-leg": "${{ toJSON(matrix) }}",
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


# Only a public upstream's dispatch (workflow_dispatch with from-jobs) posts check
# runs; reusable-workflow calls already show on the PR.
_CHECK_RUN_WHEN: Final = "github.event_name == 'workflow_dispatch' && inputs.from-jobs != '[]'"
_REPORT_CHECK_RUN_ACTION: Final = "ecmwf/ci-infrastructure/actions/report-check-run@main"
_ANNOUNCE_IMAGE_ACTION: Final = "ecmwf/ci-infrastructure/actions/announce-image@main"
_RUN_URL: Final = "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"


def _announce_image_step() -> Step:
    """Log the container image; a no-op outside our images."""
    return {
        "name": "Announce image",
        "uses": _ANNOUNCE_IMAGE_ACTION,
    }


def _check_run_step(check_name: str, phase: Literal["start", "finish"]) -> Step:
    """Report this job as a check run on the dispatcher's commit."""
    step: Step = {"id": "check_start"} if phase == "start" else {"name": "Report check-run conclusion"}
    step["if"] = "${{ " + ("always() && " if phase == "finish" else "") + _CHECK_RUN_WHEN + " }}"
    step["uses"] = _REPORT_CHECK_RUN_ACTION
    step["with"] = {
        "token": "${{ steps.mint.outputs.token }}",
        "head-repo": "${{ inputs.from-repo }}",
        "head-sha": "${{ inputs.from-sha }}",
        "name": check_name,
        "details-url": _RUN_URL,
        "phase": phase,
    }
    if phase == "finish":
        step["with"]["conclusion"] = "${{ job.status }}"
        step["with"]["check-run-id"] = "${{ steps.check_start.outputs.check-run-id }}"
    return step


def _ctest_step(mk: MatrixKind) -> Step:
    """ctest on the build tree, before publishing: the store cannot tell a tested artifact from an untested one."""
    cmd = 'ctest --test-dir "${{ steps.build.outputs.build-dir }}" --output-on-failure'
    if mk.ctest_args:
        cmd = f"{cmd} {mk.ctest_args}"
    return {"name": "Test", "run": cmd}


def _kind_job(m: Manifest, kind: str, cross: Sequence[JobRef]) -> dict[str, Any]:
    """One job per kind: mint → checkout → decode → (setup-python) → fetch → build → (test) → (publish)."""
    mk = m.matrices[kind]
    display = f"{m.package_name}/{kind}"

    local_needs = [n for n in mk.needs if "/" not in n]
    needs_list = ["resolve"] + [job_id(m.package_name, n) for n in local_needs]

    cond = _render_kind_filter(cross, mk.triggers)

    # The slots come from resolve_deps (job_names), shared with the repo's own ci.yml.
    job_name = f"{display} (${{{{ matrix._resolved['job-name'] }}}})"

    steps: list[Step] = [
        _mint_step(),
        _check_run_step(job_name, "start"),
        # Before checkout, so an early failure still names the image.
        _announce_image_step(),
        {
            "uses": "actions/checkout@v6",
            "with": {
                "repository": m.repo,
                "ref": "${{ needs.resolve.outputs.ref }}",
                "token": "${{ steps.mint.outputs.token }}",
                **({"submodules": m.submodules} if m.submodules else {}),
            },
        },
        _decode_step(mk),
    ]
    is_hpc = mk.execution == EXECUTION_HPC
    if not is_hpc:
        setup_py = _setup_python_step(mk)
        if setup_py is not None:
            steps.append(setup_py)
    fetch_with = {
        "deps-json": "${{ steps.m.outputs.deps-json }}",
        "token": "${{ steps.mint.outputs.token }}",
    }
    if is_hpc:
        # The job script installs wheels on the compute node.
        fetch_with["install-python-deps"] = "false"
    steps.append(
        {
            "name": "Fetch resolved deps",
            "id": "deps",
            "uses": "ecmwf/ci-infrastructure/actions/fetch-deps@main",
            "with": fetch_with,
        }
    )
    if is_hpc:
        steps.append(_hpc_build_step(mk))
    else:
        steps.append(_action_call_step(mk))
        if mk.publishes:
            if mk.ctest:
                steps.append(_ctest_step(mk))
            steps.append(
                {
                    "name": "Publish",
                    "uses": "ecmwf/ci-infrastructure/actions/publish-artifact@main",
                    "with": {
                        "install-path": "${{ steps.build.outputs.install-path }}",
                        "artifact-name": "${{ steps.m.outputs.own-artifact-name }}",
                    },
                }
            )
    steps.append(_check_run_step(job_name, "finish"))

    job: dict[str, Any] = {
        "needs": needs_list,
        "if": cond,
        "name": job_name,
        "runs-on": "${{ matrix['runs-on'] }}",
    }
    # An empty image runs on the host. HPC legs need it too: the ssh identity lives in the image.
    container: dict[str, Any] = {"image": "${{ matrix.container || '' }}"}
    if mk.container_credentials:
        container["credentials"] = {
            "username": "${{ secrets.ECCR_PULL_ROBOT_NAME }}",
            "password": "${{ secrets.ECCR_PULL_ROBOT_TOKEN }}",
        }
    job["container"] = container
    job["strategy"] = {
        "fail-fast": False,
        "matrix": f"${{{{ fromJSON(needs.resolve.outputs.matrix-{kind}) }}}}",
    }
    job["steps"] = steps
    return job


def compute_transitive_consumers(
    manifests: Sequence[Manifest],
) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Per repo: `pkg/kind` → transitive `consumers` and the `expected-checks` they run.

    Flattened at generation time so the orchestrator calls every consumer at depth 1.
    """
    by_pkg = {m.package_name: m for m in manifests}
    by_repo = {m.repo: m for m in manifests}

    direct: dict[tuple[str, str], set[str]] = defaultdict(set)
    for m in manifests:
        for mk in m.matrices.values():
            for need in mk.needs:
                pkg, target_kind = _split_need(need)
                if pkg is None:
                    continue
                direct[(pkg, target_kind)].add(m.repo)

    trigger_out: dict[str, list[str]] = {m.repo: [t.repo for t in m.triggers if t.repo in by_repo] for m in manifests}

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
    """{consumer: ref} over m's trigger closure; SchemaError if two paths disagree."""
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


# GHA limits per top-level run, checked at generation time, per lane:
#   1. 256 jobs; called workflows expand inside the orchestrator's run (220 keeps a margin).
#   2. 4 nesting levels; the flat closure fixes our depth at 2.
#   3. 20 distinct reusable workflows, i.e. consumer packages.
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
    """trigger-downstream{-hpc}.yml: on a successful CI run, call each in-lane consumer and post `downstream/<lane>`.

    None if `m` has no in-lane consumers. `uses:@<ref>` pins only the workflow
    definition; the consumer's pick-ref still branch-matches the code.
    """
    self_closure = closures.get(m.repo, {})
    if not self_closure:
        return None

    consumer_refs = resolve_consumer_refs(m, by_repo)

    # cpkg -> in-lane `pkg/kind` originators reaching it; one call carries them all.
    origins: dict[str, set[str]] = {}
    for orig_key in sorted(self_closure.keys()):
        okind = orig_key.split("/", 1)[1]
        if m.matrices[okind].execution != lane:
            continue
        for r in self_closure[orig_key]["consumers"]:
            origins.setdefault(by_repo[r].package_name, set()).add(orig_key)

    if not origins:
        return None

    _check_orchestrator_caps(m, origins, by_pkg, lane=lane)

    all_consumers = sorted(origins)
    consumer_deps = _cross_package_deps(all_consumers, by_pkg, lane=lane)

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
        if _edge_needs_dispatch(m, cmanifest):
            jobs[jid] = _orchestrator_dispatch_job(cpkg, crepo, cref, from_jobs, dep_ids, lane=lane)
        else:
            jobs[jid] = _orchestrator_job(cpkg, crepo, cref, from_jobs, dep_ids, lane=lane)

    jobs["report-result"] = _report_result_job(lane, consumer_job_ids)

    label = m.downstream_gate_label
    if label:
        jobs = _apply_label_gate(jobs, label)
    jobs = _require_context(jobs)

    on: dict[str, Any] = {
        "workflow_run": {
            "workflows": ["CI"],
            "types": ["completed"],
        },
    }
    doc: dict[str, Any] = {
        "name": f"Downstream {_lane_label(lane)} ({m.package_name})",
        "on": on,
        # Concurrency cannot see `needs`, so the commit comes from the event.
        "concurrency": {
            "group": f"trigger-downstream-{lane}-" + "${{ github.event.workflow_run.head_sha }}",
            "cancel-in-progress": True,
        },
        "jobs": jobs,
    }
    return m.generated_header + GENERATED_HEADER + _dump_workflow(doc)


_CONTEXT_JOB_ID: Final = "context"

_HEAD_SHA: Final = f"${{{{ needs.{_CONTEXT_JOB_ID}.outputs.head-sha }}}}"
_HEAD_BRANCH: Final = f"${{{{ needs.{_CONTEXT_JOB_ID}.outputs.head-branch }}}}"
_CI_URL: Final = f"${{{{ needs.{_CONTEXT_JOB_ID}.outputs.ci-url }}}}"
_CI_SUMMARY: Final = f"${{{{ needs.{_CONTEXT_JOB_ID}.outputs.ci-summary }}}}"
_CI_CONCLUSION: Final = f"needs.{_CONTEXT_JOB_ID}.outputs.ci-conclusion"

_SUCCESS_GATE: Final = f"${{{{ {_CI_CONCLUSION} == 'success' }}}}"


def _post_status_script(*, state: str, context: str, target_url: str, description: str, preamble: str = "") -> str:
    """`gh api` posting one commit status; `state` and `description` arrive shell-quoted."""
    return (
        preamble + "gh api -X POST \\\n"
        '  "/repos/${{ github.repository }}/statuses/' + _HEAD_SHA + '" \\\n'
        f"  -f state={state} \\\n"
        f"  -f context='{context}' \\\n"
        f'  -f target_url="{target_url}" \\\n'
        f"  -f description={description}\n"
    )


def _status_job(*, when: str, step_name: str, script: str, needs: Sequence[str] = ()) -> dict[str, Any]:
    """A job posting one `downstream/<lane>` commit status with github.token.

    Exactly one final status per completed CI run: report-result or report-ci-failure.
    """
    job: dict[str, Any] = {}
    if needs:
        job["needs"] = list(needs)
    job["if"] = when
    job["runs-on"] = SLIM_RUNNER
    job["permissions"] = {"statuses": "write"}
    job["steps"] = [
        {"name": step_name, "env": {"GH_TOKEN": "${{ github.token }}"}, "run": _BlockScalar(script)},
    ]
    return job


def _report_start_job(lane: Execution) -> dict[str, Any]:
    """`pending`, before any consumer starts."""
    return _status_job(
        when=_SUCCESS_GATE,
        step_name="Post pending downstream status",
        script=_post_status_script(
            state="pending",
            context=_status_context(lane),
            target_url=_RUN_URL,
            description=f"'Downstream {_lane_label(lane)} tests running'",
        ),
    )


def _report_ci_failure_job(lane: Execution) -> dict[str, Any]:
    """`failure` linking the CI run, when CI did not succeed (the complement of _SUCCESS_GATE)."""
    return _status_job(
        when=f"${{{{ {_CI_CONCLUSION} != 'success' }}}}",
        step_name="Post downstream failure status",
        script=_post_status_script(
            state="failure",
            context=_status_context(lane),
            target_url=_CI_URL,
            description=f'"{_CI_SUMMARY}; downstream {_lane_label(lane)} not run"',
        ),
    )


def _report_result_job(lane: Execution, consumer_job_ids: Sequence[str]) -> dict[str, Any]:
    """`failure` if validate or any consumer failed or was cancelled, else `success`."""
    needs = ["validate", *consumer_job_ids]
    results = " ".join(f"${{{{ needs.{jid}.result }}}}" for jid in needs)
    return _status_job(
        needs=needs,
        when=f"${{{{ always() && {_CI_CONCLUSION} == 'success' }}}}",
        step_name="Post final downstream status",
        script=_post_status_script(
            preamble=(
                "state=success\n"
                f"for r in {results}; do\n"
                '  if [ "$r" = failure ] || [ "$r" = cancelled ]; then\n'
                "    state=failure\n"
                "  fi\n"
                "done\n"
            ),
            state='"$state"',
            context=_status_context(lane),
            target_url=_RUN_URL,
            description=f'"Downstream {_lane_label(lane)} tests $state"',
        ),
    )


def _orchestrator_job_id(consumer_pkg: str) -> str:
    """One job per consumer package, so no kind suffix."""
    return _id_segment(consumer_pkg)


def _edge_needs_dispatch(caller: Manifest, consumer: Manifest) -> bool:
    """True iff a public upstream fans out to a private consumer.

    A called workflow's jobs log into the caller's public run, so this edge is
    dispatched instead; the orchestrator waits on the run's conclusion only, and
    the consumer's jobs post check runs back.
    """
    return caller.visibility == VISIBILITY_PUBLIC and consumer.visibility == VISIBILITY_PRIVATE


def _cross_package_deps(
    consumer_pkgs: Sequence[str], by_pkg: Mapping[str, Manifest], *, lane: Execution
) -> dict[str, set[str]]:
    """In-scope consumer packages each consumer needs via its `lane` kinds."""
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


_GATE_JOB_ID: Final = "label-gate"
# Index syntax, not `needs.label-gate`: a hyphen in a context path parses as minus.
_GATE_PASSED: Final = f"needs['{_GATE_JOB_ID}'].outputs.run == 'true'"


def _prepend_need(job: dict[str, Any], jid: str) -> None:
    needs = job.get("needs", [])
    job["needs"] = [jid, *(needs if isinstance(needs, list) else [needs])]


def _apply_label_gate(jobs: dict[str, Any], label: str) -> dict[str, Any]:
    """Make every job need `label-gate` and AND its verdict into the job's `if:`.

    A post-pass, so a job added later is gated by construction.
    """
    gated: dict[str, Any] = {_GATE_JOB_ID: _label_gate_job(label)}
    for jid, job in jobs.items():
        _prepend_need(job, _GATE_JOB_ID)
        cond = job.get("if")
        inner = cond[3:-2].strip() if isinstance(cond, str) and cond.startswith("${{") else None
        job["if"] = f"${{{{ ({inner}) && {_GATE_PASSED} }}}}" if inner else f"${{{{ {_GATE_PASSED} }}}}"
        gated[jid] = job
    return gated


def _require_context(jobs: dict[str, Any]) -> dict[str, Any]:
    """Make every job need `context` (a post-pass, like _apply_label_gate).

    No approval gate: fork code is kept out because work jobs demand a green CI
    run on the head SHA, which had to pass its own approval gate.
    """
    ordered: dict[str, Any] = {_CONTEXT_JOB_ID: _context_job()}
    for jid, job in jobs.items():
        _prepend_need(job, _CONTEXT_JOB_ID)
        ordered[jid] = job
    return ordered


def _context_job() -> dict[str, Any]:
    """`context`: which commit this run is about, and whether its CI passed."""
    return {
        "runs-on": SLIM_RUNNER,
        "permissions": {"actions": "read"},
        "outputs": {
            "head-sha": "${{ steps.ctx.outputs.head-sha }}",
            "head-branch": "${{ steps.ctx.outputs.head-branch }}",
            "ci-conclusion": "${{ steps.ctx.outputs.ci-conclusion }}",
            "ci-url": "${{ steps.ctx.outputs.ci-url }}",
            "ci-summary": "${{ steps.ctx.outputs.ci-summary }}",
        },
        "steps": [
            {
                "name": "Resolve the commit under test",
                "id": "ctx",
                "uses": "ecmwf/ci-infrastructure/actions/resolve-dispatch-context@main",
            },
        ],
    }


def _label_gate_job(label: str) -> dict[str, Any]:
    """`label-gate`: does the commit's open PR carry the label (a push always runs)?

    Not gated on CI success: report-ci-failure needs it too. Like every job that
    reads or writes its own repo, it uses github.token with explicit
    `permissions`; an App token 403s on non-public repos.
    """
    return {
        "runs-on": SLIM_RUNNER,
        "permissions": {"pull-requests": "read"},
        "outputs": {"run": "${{ steps.gate.outputs.run }}"},
        "steps": [
            {
                "name": "Check the downstream-CI label",
                "id": "gate",
                "uses": "ecmwf/ci-infrastructure/actions/check-pr-label@main",
                "with": {
                    "label": label,
                    "sha": _HEAD_SHA,
                },
            },
        ],
    }


def _validate_job() -> dict[str, Any]:
    """Check the generated workflows match the manifest at the tested SHA.

    allow-unsafe-pr-checkout is safe only because nothing checked out is executed;
    never copy it to a job that builds or runs the checkout.
    """
    return {
        "if": _SUCCESS_GATE,
        "runs-on": SLIM_RUNNER,
        "steps": [
            _mint_step(),
            {
                "uses": "actions/checkout@v6",
                "with": {
                    "ref": _HEAD_SHA,
                    "token": "${{ steps.mint.outputs.token }}",
                    "allow-unsafe-pr-checkout": True,
                },
            },
            {
                "uses": "ecmwf/ci-infrastructure/actions/validate-generated-workflows@main",
                "with": {"token": "${{ steps.mint.outputs.token }}"},
            },
        ],
    }


def _consumer_inputs(cref: str, from_jobs: Sequence[str]) -> dict[str, str]:
    return {
        "from-repo": "${{ github.repository }}",
        "from-sha": _HEAD_SHA,
        "from-jobs": json.dumps(sorted(from_jobs), separators=(",", ":")),
        "branch": _HEAD_BRANCH,
        "fallback-ref": cref,
    }


def _orchestrator_job(
    cpkg: str,
    crepo: str,
    cref: str,
    from_jobs: Sequence[str],
    dep_job_ids: Sequence[str],
    *,
    lane: Execution,
) -> dict[str, Any]:
    """Call the consumer's cross-repo-trigger{-hpc}.yml as a reusable workflow."""
    return {
        "name": cpkg,
        "needs": ["validate", *dep_job_ids],
        "uses": f"{crepo}/.github/workflows/cross-repo-trigger{lane_suffix(lane)}.yml@{cref}",
        "with": _consumer_inputs(cref, from_jobs),
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
    """Dispatch a private consumer (see _edge_needs_dispatch) and wait for its run's conclusion."""
    dispatch_with: dict[str, Any] = {"consumer-repo": crepo}
    if lane == EXECUTION_HPC:
        dispatch_with["workflow-file"] = f"cross-repo-trigger{lane_suffix(lane)}.yml"
    dispatch_with["ref"] = cref
    dispatch_with.update(_consumer_inputs(cref, from_jobs))
    dispatch_with.update(
        {
            "token": "${{ steps.mint.outputs.token }}",
            "artifact-names": "",
            "wait-for-run-conclusion": "true",
        }
    )
    return {
        "name": cpkg,
        "needs": ["validate", *dep_job_ids],
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
    origins: Mapping[str, set[str]],
    by_pkg: Mapping[str, Manifest],
    *,
    lane: Execution,
) -> None:
    """Estimate the lane's job count and distinct reusable workflows against the GHA caps."""
    estimated_jobs = 4  # validate + report-start + report-result + report-ci-failure
    distinct_consumers = set(origins)
    for cpkg, orig_keys in origins.items():
        cmanifest = by_pkg[cpkg]
        estimated_jobs += 2  # caller job + resolve
        for ckind, mk in cmanifest.matrices.items():
            if TRIGGER_UPSTREAM_CHANGE not in mk.triggers or mk.execution != lane:
                continue
            refs = transitive_cross_repo_needs(cmanifest, ckind, by_pkg)
            if any(f"{r.package}/{r.kind}" in orig_keys for r in refs):
                estimated_jobs += max(1, len(mk.legs))

    if estimated_jobs > ORCHESTRATOR_MAX_TOTAL_JOBS:
        raise SchemaError(
            f"{m.path}: orchestrator for {m.package_name} would expand to ~{estimated_jobs} jobs, "
            f"exceeding the safety limit of {ORCHESTRATOR_MAX_TOTAL_JOBS} (GHA hard cap is 256 "
            f"jobs per workflow run). Reduce matrix density or split via tiered fan-out."
        )

    if len(distinct_consumers) > ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS:
        raise SchemaError(
            f"{m.path}: orchestrator for {m.package_name} would call {len(distinct_consumers)} distinct "
            f"reusable workflows, exceeding GHA's limit of {ORCHESTRATOR_MAX_REUSABLE_WORKFLOWS} reusable "
            f"workflows callable from one top-level workflow run. Split the fan-out into tiers."
        )


_FETCH_DEPTH_CAP: Final = 8


def _local_sibling_layer(
    layer: Sequence[tuple[str, str]],
    sibling_root: Path,
    manifest_path: str,
) -> dict[tuple[str, str], tuple[str | None, bool]]:
    """fetch_manifests_layer from the working trees under `sibling_root`; refs are ignored."""
    out: dict[tuple[str, str], tuple[str | None, bool]] = {}
    for repo, ref in layer:
        candidate = sibling_root / repo.split("/")[-1] / manifest_path
        out[(repo, ref)] = (candidate.read_text() if candidate.is_file() else None, False)
    return out


def _fetch_sibling_manifests(
    local: Manifest,
    token: str | None,
    manifest_path: str,
    sibling_root: Path | None = None,
) -> list[Manifest]:
    """`local` first, then every manifest reachable via deps and triggers; missing ones are skipped."""

    def neighbours(m: Manifest) -> list[tuple[str, str]]:
        return [(d.repo, "HEAD") for d in m.deps] + [(t.repo, t.ref or "HEAD") for t in m.triggers]

    seen: dict[str, Manifest] = {local.repo: local}
    queue = neighbours(local)

    for _ in range(_FETCH_DEPTH_CAP):
        layer = [(r, ref) for (r, ref) in queue if r not in seen]
        if not layer:
            break
        if sibling_root is not None:
            results = _local_sibling_layer(layer, sibling_root, manifest_path)
        else:
            results = fetch_manifests_layer(layer, sync_branch=None, token=token, manifest_path=manifest_path)
        next_q: list[tuple[str, str]] = []
        for (repo, ref), (text, _sync) in results.items():
            if text is None:
                continue
            m = parse_manifest_text(text, Path(f"github://{repo}@{ref}/{manifest_path}"))
            seen[repo] = m
            next_q.extend(neighbours(m))
        queue = next_q

    # An unreadable trigger target silently shrinks the fan-out; warn.
    unresolved = sorted({t.repo for t in local.triggers if t.repo not in seen})
    if unresolved:
        where = f"under {sibling_root}" if sibling_root is not None else "at the ref it declares"
        for repo in unresolved:
            print(
                f"::warning::[[trigger-downstream]] target {repo} has no readable "
                f"{manifest_path} {where}, so it contributes no jobs to this repo's "
                "orchestrator. Check the repo name; if the manifest simply is not on "
                "that ref yet (a coordinated change still on branches), pass "
                "--sibling-root <dir> to read the sibling clones instead.",
                file=sys.stderr,
            )

    return [local] + sorted((m for m in seen.values() if m.repo != local.repo), key=lambda m: m.repo)


@dataclass(frozen=True)
class Change:
    """One generated file that is not what the manifest says it should be."""

    action: Literal["delete", "update", "create"]
    path: Path
    where: Sequence[str] = ()  # where the parsed documents disagree

    def render(self) -> str:
        lines = [f"  - [{self.action}] {self.path}"]
        lines += [f"      {w}" for w in self.where]
        return "\n".join(lines)


_MAX_DIFF_LOCATIONS: Final = 6


def _yaml_diff_locations(rendered: Any, checked_in: Any, path: str = "") -> list[str]:
    """Where two parsed workflow documents disagree, as dotted paths."""
    label = path or "<root>"
    if type(rendered) is not type(checked_in):
        return [f"{label}: {type(rendered).__name__} vs {type(checked_in).__name__}"]
    out: list[str] = []
    if isinstance(rendered, dict):
        for key in sorted(set(rendered) | set(checked_in), key=str):
            if key not in checked_in:
                out.append(f"{path}.{key}: only in the rendered output")
            elif key not in rendered:
                out.append(f"{path}.{key}: only in the checked-in file")
            else:
                out += _yaml_diff_locations(rendered[key], checked_in[key], f"{path}.{key}")
            if len(out) >= _MAX_DIFF_LOCATIONS:
                break
    elif isinstance(rendered, list):
        if len(rendered) != len(checked_in):
            out.append(f"{label}: {len(rendered)} entries rendered, {len(checked_in)} checked in")
        for i, (a, b) in enumerate(zip(rendered, checked_in)):
            out += _yaml_diff_locations(a, b, f"{path}[{i}]")
            if len(out) >= _MAX_DIFF_LOCATIONS:
                break
    elif rendered != checked_in:
        out.append(f"{label}: {rendered!r} rendered, {checked_in!r} checked in")
    return out[:_MAX_DIFF_LOCATIONS]


def _write_or_check_path(out: Path, content: str | None, check: bool) -> tuple[bool, list[str]]:
    """Write, delete (`content=None`) or --check one file; returns (changed, where).

    A write compares text; --check compares parsed documents.
    """
    existing = out.read_text() if out.exists() else None
    if content is None:
        if existing is None:
            return False, []
        if not check:
            out.unlink()
        return True, []
    if existing == content:
        return False, []
    if check:
        if existing is None:
            return True, []
        try:
            rendered_doc, existing_doc = _parse_workflow(content), _parse_workflow(existing)
        except yaml.YAMLError as exc:
            return True, [f"cannot parse the checked-in file: {str(exc).splitlines()[0]}"]
        if rendered_doc == existing_doc:
            return False, []
        return True, _yaml_diff_locations(rendered_doc, existing_doc)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content)
    return True, []


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
    help="Don't write; report any generated file that is stale.",
)
@click.option(
    "--fail-on-drift",
    "fail_on_drift",
    is_flag=True,
    help="With --check, exit 1 on drift instead of only warning.",
)
@click.option(
    "--sibling-root",
    "sibling_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Read sibling manifests from clones under this directory (<root>/<repo-name>) "
    "instead of GitHub, for a coordinated change whose sibling manifests are not on "
    "their default branches yet.",
)
def main(manifest_path: str, check: bool, fail_on_drift: bool, sibling_root: Path | None) -> None:
    _run(manifest_path, check, sibling_root, fail_on_drift=fail_on_drift)


def _run(
    manifest_path: str,
    check: bool,
    sibling_root: Path | None = None,
    *,
    fail_on_drift: bool = False,
) -> None:
    if fail_on_drift and not check:
        raise CIError(
            "--fail-on-drift only applies with --check; without it, drift is written away rather than reported"
        )
    local_manifest_path = Path(manifest_path)
    if not local_manifest_path.is_file():
        raise CIError(f"no manifest found at {local_manifest_path}")

    try:
        local = parse_manifest(local_manifest_path)
        validate_job_templates(local)
        # No env token is fine: gh falls back to its own auth.
        manifests = _fetch_sibling_manifests(local, select_token(), manifest_path, sibling_root)
        validate_graph(manifests)
        closures = compute_transitive_consumers(manifests)
        by_pkg = {m.package_name: m for m in manifests}
        by_repo = {m.repo: m for m in manifests}
        # Siblings only feed validation and the closure; render the local repo alone.
        changed = _render_one_repo(local, by_pkg, by_repo, closures, check)
    except SchemaError as e:
        raise CIError(str(e)) from e

    _report(changed, check, manifest_path, fail_on_drift=fail_on_drift)


def _render_one_repo(
    m: Manifest,
    by_pkg: Mapping[str, Manifest],
    by_repo: Mapping[str, Manifest],
    closures: Mapping[str, dict[str, dict[str, list[str]]]],
    check: bool,
) -> list[Change]:
    changed: list[Change] = []
    wf_dir = m.repo_root / ".github" / "workflows"

    for lane in (EXECUTION_RUNNER, EXECUTION_HPC):
        suffix = lane_suffix(lane)
        for basename, content in (
            (f"cross-repo-trigger{suffix}.yml", render_workflow(m, by_pkg, lane=lane)),
            (
                f"trigger-downstream{suffix}.yml",
                render_orchestrator_workflow(m, by_pkg, by_repo, closures, lane=lane),
            ),
        ):
            path = wf_dir / basename
            existed = path.exists()
            needed, where = _write_or_check_path(path, content, check)
            if needed:
                if content is None:
                    changed.append(Change("delete", path, where))
                else:
                    changed.append(Change("update" if existed else "create", path, where))

    return changed


def _regen_command(manifest_path: str) -> str:
    """The canonical invocation that writes instead of checking."""
    parts: list[str] = ["ci-infrastructure-generate"]
    if manifest_path != ".ci/manifest.toml":
        parts += ["--manifest-path", manifest_path]
    return shlex.join(parts)


def _drift_message(changed: Sequence[Change], manifest_path: str, *, fatal: bool) -> str:
    """The out-of-date report, headline first, then the regen command."""
    stale = "\n".join(c.render() for c in changed)
    tail = (
        ""
        if fatal
        else "\nThis is a warning. Set fail-on-drift on the validate-generated-workflows\n"
        "action (or pass --fail-on-drift) to make it a failure.\n"
    )
    return (
        "generated files are out of date:\n"
        f"{stale}\n"
        "\n"
        "To regenerate, run from this repo (drops --check, writes in place):\n"
        "\n"
        "  gh auth login        # only needed if 'gh' isn't already authenticated\n"
        f"  {_regen_command(manifest_path)}\n"
        "\n"
        "ci-infrastructure-generate is installed by `pip install ci-infrastructure`\n"
        "(https://github.com/ecmwf/ci-infrastructure); inside a job the composite\n"
        'actions expose it as "$CI_INFRASTRUCTURE_PYTHON" -m ci_infrastructure.generate_downstream_ci.\n'
        "Then commit the regenerated files alongside your manifest changes.\n" + tail
    )


def _report(changed: Sequence[Change], check: bool, manifest_path: str, *, fail_on_drift: bool) -> None:
    """Print what changed; under --check, drift is a warning unless `fail_on_drift`.

    A warning by default: one repo regenerated without its sibling should not
    withhold downstream CI from every consumer.
    """
    if not check:
        for c in changed:
            print(f"{c.action} {c.path}")
        return
    if not changed:
        return
    message = _drift_message(changed, manifest_path, fatal=fail_on_drift)
    if fail_on_drift:
        raise CIError(message)
    # A workflow command only covers its first line.
    head, _, rest = message.partition("\n")
    print(f"::warning::{head}", file=sys.stderr)
    if rest:
        print(rest, file=sys.stderr)


if __name__ == "__main__":
    main()
