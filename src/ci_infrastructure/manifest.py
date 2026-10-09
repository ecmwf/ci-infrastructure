# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The schema of `.ci/manifest.toml`: one model per TOML table.

Cross-manifest rules (the `needs` graph, trigger cycles) live in `generate_downstream_ci`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from ._github_api import (
    _OPTION_TOKEN_RE,
    EXECUTION_HPC_ATOS,
    EXECUTION_RUNNER,
    Execution,
    ManifestSchemaError,
)

Visibility: TypeAlias = Literal["public", "private"]
VISIBILITY_PUBLIC: Final[Visibility] = "public"
VISIBILITY_PRIVATE: Final[Visibility] = "private"

_LANES: Final = frozenset({EXECUTION_RUNNER, EXECUTION_HPC_ATOS})

TRIGGER_UPSTREAM_CHANGE: Final = "upstream-change"
TRIGGER_REBUILD_REQUEST: Final = "rebuild-request"
_VALID_TRIGGERS: Final = frozenset({TRIGGER_UPSTREAM_CHANGE, TRIGGER_REBUILD_REQUEST})

_ARTIFACT_PREFIX_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_repo(v: str) -> str:
    if v.count("/") != 1 or not all(v.split("/", 1)):
        raise ValueError(f"repo must be 'owner/name', got {v!r}")
    return v


def _check_install_dirs(v: tuple[str, ...]) -> tuple[str, ...]:
    for d in v:
        parts = PurePosixPath(d).parts
        if not d or d.startswith("/") or ".." in parts or any(c in d for c in ":;$`\\\n"):
            raise ValueError(f"'add-to-path' takes directories inside the install tree, e.g. \"bin\"; got {d!r}")
    return v


def _check_compiler_inputs(v: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(s.strip() for s in v)
    if any(not s for s in cleaned):
        raise ValueError(f"'compiler-inputs' contains an empty entry: {list(v)!r}")
    return cleaned


class _Table(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PackageTable(_Table):
    """`[package]`: this repo's own package."""

    name: str = Field(
        min_length=1,
        description="Package name, unique across the graph. Other manifests' `needs` refer to it as `<name>/<kind>`.",
    )
    prefix: str = Field(
        default="",
        min_length=1,
        description="Artifact-name prefix, `name` when absent. Consumers name it in `[[deps]].package`.",
    )
    repo: str | None = Field(
        default=None,
        description="`owner/name` of this repo. The resolver falls back to `--self-repo`; the generator requires it.",
    )
    compiler_inputs: tuple[str, ...] = Field(
        alias="compiler-inputs",
        description="Leg fields naming the compilers in this package's artifact name, joined in alphabetical "
        "field-name order. `[]` for an uncompiled package.",
    )
    visibility: Visibility = Field(
        default=VISIBILITY_PRIVATE,
        description="`public` or `private`; unlabelled repos are private. A public repo triggers a private "
        "consumer by dispatch rather than `workflow_call`. Set it only once the security and exposure risks "
        "are understood, see :doc:`/configuring/manifest`.",
    )
    submodules: Literal["true", "recursive"] | None = Field(
        default=None, description="`actions/checkout` `submodules` for the build jobs."
    )
    meta: bool = Field(
        strict=True,
        default=False,
        description="An umbrella package: no artifact of its own. A dep on it stands for its `[[deps]]`.",
    )
    add_to_path: tuple[str, ...] = Field(
        default=(),
        alias="add-to-path",
        description="Directories of the install tree, relative to it, that a consumer's build job puts on `PATH`, "
        'e.g. `["bin"]` for tools the consumer\'s tests run. Nothing by default.',
    )
    git_read: tuple[str, ...] = Field(
        default=(),
        alias="git-read",
        description="`owner/name` of repos the runner build fetches over git, e.g. a cargo git dependency. "
        "The job gets a token that can only read these; same owner as `repo`.",
    )

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, v: str | None) -> str | None:
        return v if v is None else _check_repo(v)

    @field_validator("git_read")
    @classmethod
    def _git_read_shape(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_check_repo(r) for r in v)

    @model_validator(mode="after")
    def _git_read_same_owner(self) -> PackageTable:
        owner = (self.repo or "").split("/", 1)[0]
        foreign = [r for r in self.git_read if self.repo and r.split("/", 1)[0] != owner]
        if foreign:
            raise ValueError(f"git-read {foreign!r}: one app token covers one owner, '{owner}'")
        return self

    @field_validator("compiler_inputs")
    @classmethod
    def _compiler_inputs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_compiler_inputs(v)

    @field_validator("add_to_path")
    @classmethod
    def _add_to_path(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_install_dirs(v)

    @model_validator(mode="after")
    def _prefix_defaults_to_name(self) -> PackageTable:
        self.prefix = self.prefix or self.name
        return self


class DepTable(_Table):
    """`[[deps]]`: one upstream package whose artifact every applicable leg fetches."""

    repo: str | None = Field(
        default=None, description="`owner/name` of the upstream repo; absent for a package of this repo."
    )
    package: str | tuple[str, ...] = Field(
        description="The upstream's `[package].prefix`; a list declares one dep per package, sharing the other fields."
    )
    ref: str | None = Field(
        default=None,
        description="Branch, tag or 40-char SHA; required with `repo`. A `sync-branch/` or `feature/` branch of the "
        "same name in the upstream overrides it. A package of this repo is built from the same commit.",
    )
    compiler_inputs: tuple[str, ...] | None = Field(
        default=None,
        alias="compiler-inputs",
        description="Leg fields holding the upstream's compilers; must match the upstream package's "
        "`compiler-inputs`, which apply when this is omitted. `[]` if the upstream is compiler-independent.",
    )
    build_type_input: str = Field(
        default="build-type", alias="build-type-input", description="Leg field selecting the upstream's build type."
    )
    platform_input: str = Field(
        default="platform", alias="platform-input", description="Leg field selecting the upstream's platform."
    )
    python_version_input: str = Field(
        default="python-version",
        alias="python-version-input",
        description="Leg field selecting the upstream's Python version; read only with `needs-python`.",
    )
    needs_python: bool = Field(
        strict=True,
        default=False,
        alias="needs-python",
        description="The artifact carries a wheel; fetch-deps installs it into the consumer's Python.",
    )
    options: str = Field(
        default="",
        description="Build option of the upstream artifact to consume, one name per CMake preset. "
        "Options do not propagate: without this or `options-input` the plain build is used.",
    )
    options_input: str | None = Field(
        default=None, alias="options-input", description="Leg field to read the upstream's build option from."
    )
    when: dict[str, tuple[str, ...]] | None = Field(
        default=None,
        description="Leg predicate: the dep applies only to legs where every field has one of the accepted "
        "values, compared as strings. A scalar is a one-element list. `execution` is the leg's lane "
        "(`runner` or `hpc-atos`). A scoped-out dep is absent from the leg's `cmake-prefix-path` and its deps hash.",
    )
    unless: dict[str, tuple[str, ...]] | None = Field(
        default=None,
        description="The negation of `when`, in the same form: the dep does not apply to legs where every field "
        "has one of the listed values.",
    )

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, v: str | None) -> str | None:
        return v if v is None else _check_repo(v)

    @field_validator("compiler_inputs")
    @classmethod
    def _compiler_inputs(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return v if v is None else _check_compiler_inputs(v)

    @field_validator("ref")
    @classmethod
    def _ref_nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return v
        s = v.strip()
        if not s:
            raise ValueError('must be a branch, tag or 40-char SHA, e.g. "main"')
        return s

    @model_validator(mode="after")
    def _ref_goes_with_repo(self) -> DepTable:
        if self.repo is not None and self.ref is None:
            raise ValueError("needs a 'ref' with its 'repo'")
        if self.repo is None and self.ref is not None:
            raise ValueError("'ref' without 'repo': a package of this repo is built from the same commit")
        return self

    @property
    def packages(self) -> tuple[str, ...]:
        return (self.package,) if isinstance(self.package, str) else self.package

    @field_validator("options", mode="before")
    @classmethod
    def _option_scalar(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            raise ValueError(f"must be a scalar config name, not a list ({v!r}); name the combination (e.g. 'a-b')")
        if isinstance(v, str) and v and not _OPTION_TOKEN_RE.fullmatch(v):
            raise ValueError(f"invalid build option {v!r}: only [A-Za-z0-9_-] allowed")
        return v

    @field_validator("package", mode="before")
    @classmethod
    def _package_shape(cls, v: Any) -> Any:
        if isinstance(v, str):
            if not v:
                raise ValueError("must not be empty")
            return v
        if not isinstance(v, (list, tuple)) or not v or not all(isinstance(x, str) and x for x in v):
            raise ValueError(f"must be a package prefix or a non-empty list of them, got {v!r}")
        if len(set(v)) != len(v):
            raise ValueError(f"lists a package twice: {list(v)}")
        return tuple(v)

    @field_validator("when", "unless", mode="before")
    @classmethod
    def _predicate_shape(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None:
            return v
        name = info.field_name
        if not isinstance(v, dict) or not v:
            raise ValueError(
                f'must be a non-empty table of matrix field to accepted value(s), e.g. {{ options = ["extended"] }}; '
                f"got {v!r}"
            )
        out: dict[str, tuple[str, ...]] = {}
        for key, accepted in v.items():
            values = accepted if isinstance(accepted, (list, tuple)) else [accepted]
            if not values:
                raise ValueError(f"{name}.{key} must list at least one accepted value")
            if any(isinstance(x, (dict, list, tuple)) for x in values):
                raise ValueError(f"{name}.{key} values must be scalars, got {accepted!r}")
            if key == "execution" and (bad := sorted({str(x) for x in values} - _LANES)):
                raise ValueError(f"{name}.execution names no lane: {bad}; lanes are {sorted(_LANES)}")
            out[str(key)] = tuple(str(x) for x in values)
        return out


class PackageEntry(_Table):
    """`[packages.<prefix>]`: a further package of this repo, published under `<prefix>`."""

    compiler_inputs: tuple[str, ...] = Field(
        alias="compiler-inputs", description="As `[package].compiler-inputs`, for this package."
    )
    deps: tuple[DepTable, ...] = Field(default=(), description="As `[[deps]]`, for this package.")
    add_to_path: tuple[str, ...] = Field(
        default=(), alias="add-to-path", description="As `[package].add-to-path`, for this package."
    )

    @field_validator("compiler_inputs")
    @classmethod
    def _compiler_inputs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_compiler_inputs(v)

    @field_validator("add_to_path")
    @classmethod
    def _add_to_path(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_install_dirs(v)


class TriggerDownstreamTable(_Table):
    """`[[trigger-downstream]]`: a consumer repo this one fans out to after a completed CI run."""

    repo: str = Field(description="`owner/name` of the consumer repo.")
    ref: str = Field(description="Ref of the consumer to build.")

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


class MatrixKindTable(_Table):
    """`[matrix.<kind>]`: one job kind and its legs.

    A leg (`[[matrix.<kind>.include]]`) is free-form: every field is available to
    its recipe, the Jinja template it names in `job-script`. `platform` is required and names the
    binary-compatibility class; `build-type`, `python-version`, `options` and the
    `compiler-inputs` fields enter the artifact name; `runs-on` and `container`
    only schedule the job.
    """

    triggers: tuple[str, ...] = Field(
        default=(),
        description="Events that run this kind in `cross-repo-trigger.yml`: `upstream-change`, `rebuild-request`. "
        "Empty keeps the kind out of it.",
    )
    needs: tuple[str, ...] = Field(
        default=(),
        description="Kinds that must finish first: `<kind>` locally, `<package-name>/<kind>` across repos. "
        "Without cross-repo entries, a kind with `triggers` gets them from `[[deps]]`: per producer that triggers "
        "this repo, its triggered kind publishing the dep in the same lane.",
    )
    reuse_matrix: str | None = Field(
        default=None,
        alias="reuse-matrix",
        description="Share the legs of another kind of this manifest instead of `include`; no chaining.",
    )
    include: tuple[dict[str, Any], ...] = Field(default=(), description="The legs.")
    defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields every leg gets unless it sets them. Under `reuse-matrix` they go beneath the reused "
        "kind's own.",
    )
    execution: Execution = Field(
        default=EXECUTION_RUNNER,
        description="`runner` (a GitHub Actions job) or `hpc-atos` (a SLURM job on Atos).",
    )
    publishes: bool = Field(
        strict=True,
        default=True,
        description="Upload the install tree as an artifact; `false` for test kinds.",
    )
    artifact_prefix: str | None = Field(
        default=None,
        alias="artifact-prefix",
        description="Publish under this prefix instead of `[package].prefix`, for a secondary artifact.",
    )
    packages: tuple[str, ...] | None = Field(
        default=None,
        description="The packages this kind publishes: `[package].prefix` or keys of `[packages]`. By default "
        "`[package]`, or every `[packages]` entry when `[package]` is `meta`. With `publishes = false`, the "
        "packages it builds and tests against their deps without publishing them.",
    )
    container_credentials: bool = Field(
        strict=True,
        default=False,
        alias="container-credentials",
        description="Pull the leg's `container` with registry credentials.",
    )

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError(f"must be a table (got {type(data).__name__})")
        allowed = {f.alias or name for name, f in cls.model_fields.items()}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise ValueError(f"has unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}")
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


class GeneratedTable(_Table):
    """`[generated]`: how the generated workflows are written."""

    header: str = Field(
        default="", description="Comment lines written above the generated-file banner, e.g. a licence header."
    )

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


class DownstreamTable(_Table):
    """`[downstream]`: this repo's own fan-out to its consumers."""

    exclude: tuple[str, ...] = Field(
        default=(),
        description="Consumer packages this repo's fan-out skips, together with every consumer in it that "
        "depends on them.",
    )


class ManifestFile(_Table):
    """The whole of `.ci/manifest.toml`."""

    package: PackageTable
    deps: tuple[DepTable, ...] = ()
    packages: dict[str, PackageEntry] = Field(default_factory=dict)
    trigger_downstream: tuple[TriggerDownstreamTable, ...] = Field(default=(), alias="trigger-downstream")
    downstream: DownstreamTable | None = None
    generated: GeneratedTable | None = None
    matrix: dict[str, MatrixKindTable] = Field(default_factory=dict)

    def published_by(self, kind: str) -> tuple[str, ...]:
        """The prefixes `kind` publishes; empty for a kind that publishes nothing."""
        body = self.matrix[kind]
        if not body.publishes:
            return ()
        if body.packages is not None:
            return body.packages
        if body.artifact_prefix is not None:
            return (body.artifact_prefix,)
        return tuple(self.packages) if self.package.meta else (self.package.prefix,)

    def built_by(self, kind: str) -> tuple[str, ...]:
        """The prefixes `kind` builds: what it publishes, or its `packages` when it publishes nothing."""
        body = self.matrix[kind]
        if not body.publishes and body.packages is not None:
            return body.packages
        return self.published_by(kind)

    def deps_of(self, prefix: str) -> tuple[DepTable, ...]:
        """A package's own `deps`; `[[deps]]` for `[package]` and an `artifact-prefix`."""
        entry = self.packages.get(prefix)
        return self.deps if entry is None else entry.deps

    @model_validator(mode="after")
    def _packages_consistent(self) -> ManifestFile:
        own = self.package.prefix
        for key in self.packages:
            if not _ARTIFACT_PREFIX_RE.fullmatch(key):
                raise ValueError(f"[packages.{key}] is not a valid prefix ({_ARTIFACT_PREFIX_RE.pattern})")
            if key == own:
                raise ValueError(f"[packages.{key}] repeats [package].prefix")
        publishable = set(self.packages) | ({own} if not self.package.meta else set())
        for where, deps in [("[[deps]]", self.deps), *((f"[packages.{k}]", e.deps) for k, e in self.packages.items())]:
            for d in deps:
                if d.repo is not None:
                    continue
                unknown = [p for p in d.packages if p not in publishable]
                if unknown:
                    raise ValueError(
                        f"{where} names {unknown} of this repo, which declares no such package; "
                        f"its packages are {sorted(publishable)}"
                    )
        self._no_sibling_cycle()
        for kind, body in self.matrix.items():
            if body.packages is not None and body.artifact_prefix is not None:
                raise ValueError(f"[matrix.{kind}] sets both packages and artifact-prefix; use packages")
            if body.packages is not None:
                if not body.packages or len(set(body.packages)) != len(body.packages):
                    raise ValueError(f"[matrix.{kind}].packages must list distinct packages, got {list(body.packages)}")
                unknown = [p for p in body.packages if p not in publishable]
                if unknown:
                    raise ValueError(
                        f"[matrix.{kind}].packages names {unknown}; this repo publishes {sorted(publishable)}"
                    )
            if self.package.meta and body.publishes and not self.published_by(kind):
                raise ValueError(
                    f"[matrix.{kind}] would publish the meta package {own!r}, which has no artifact; "
                    "list its [packages] or set publishes = false"
                )
        return self

    def _no_sibling_cycle(self) -> None:
        edges = {k: [p for d in e.deps if d.repo is None for p in d.packages] for k, e in self.packages.items()}
        edges[self.package.prefix] = [p for d in self.deps if d.repo is None for p in d.packages]
        state: dict[str, int] = {}

        def walk(node: str, path: list[str]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                raise ValueError(f"packages of this repo depend on each other in a cycle: {' -> '.join(path)}")
            state[node] = 1
            for nxt in edges.get(node, []):
                walk(nxt, [*path, nxt])
            state[node] = 2

        for node in edges:
            walk(node, [node])

    @model_validator(mode="after")
    def _no_duplicate_triggers(self) -> ManifestFile:
        seen: set[str] = set()
        for t in self.trigger_downstream:
            if t.repo in seen:
                raise ValueError(f"duplicate [[trigger-downstream]] for {t.repo!r}")
            seen.add(t.repo)
        return self


def validate(data: Mapping[str, Any]) -> ManifestFile:
    """Validate a loaded manifest; the first violation raises in TOML notation (`[matrix.build].publishes ...`)."""
    try:
        return ManifestFile.model_validate(data)
    except ValidationError as exc:
        raise ManifestSchemaError(_format_validation_error(exc)) from exc


def _format_validation_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    # pydantic prefixes errors raised from our validators.
    msg = err["msg"].removeprefix("Value error, ")
    prefix = _format_loc(tuple(err["loc"]))
    sep = " " if prefix and not prefix.endswith(" ") else ""
    return f"{prefix}{sep}{msg}".rstrip()


#: `array`: `[[deps]][2]`; `subtable` absorbs the next element: `[matrix.build]`.
_LOC_HEADS: Final = {
    "trigger-downstream": "array",
    "deps": "array",
    "matrix": "subtable",
    "packages": "subtable",
    "package": "table",
    "generated": "table",
    "downstream": "table",
}


def _format_loc(loc: tuple[str | int, ...]) -> str:
    """E.g. ("deps", 2, "package") -> "[[deps]][2].package"."""
    if not loc:
        return ""
    head, rest = str(loc[0]), loc[1:]
    shape = _LOC_HEADS.get(head)
    if shape is None:
        return ".".join(str(p) for p in loc)
    if shape == "array":
        prefix = f"[[{head}]]"
        if rest and isinstance(rest[0], int):
            prefix, rest = f"{prefix}[{rest[0]}]", rest[1:]
    elif shape == "subtable" and rest:
        prefix, rest = f"[{head}.{rest[0]}]", rest[1:]
    else:
        prefix = f"[{head}]"
    if rest:
        prefix += "." + ".".join(str(p) for p in rest)
    return prefix
