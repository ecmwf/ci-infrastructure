# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The schema of `.ci/manifest.toml`: one model per TOML table.

The generator and the resolver both validate through `validate`. The rules that
span several manifests (the `needs` graph, trigger cycles) live in
`generate_downstream_ci`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator, model_validator

from ._github_api import _OPTION_TOKEN_RE, EXECUTION_RUNNER, Execution, ManifestSchemaError

Visibility: TypeAlias = Literal["public", "private"]
VISIBILITY_PUBLIC: Final[Visibility] = "public"
VISIBILITY_PRIVATE: Final[Visibility] = "private"

TRIGGER_UPSTREAM_CHANGE: Final = "upstream-change"
TRIGGER_REBUILD_REQUEST: Final = "rebuild-request"
_VALID_TRIGGERS: Final = frozenset({TRIGGER_UPSTREAM_CHANGE, TRIGGER_REBUILD_REQUEST})

# Outputs of actions/fetch-deps.
_VALID_DEPS_OUTPUTS: Final = frozenset({"cmake-prefix-path"})
_ACTION_PATH_RE: Final = re.compile(r"^\./\.github/actions/[A-Za-z0-9_-]+$")
_ARTIFACT_PREFIX_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_repo(v: str) -> str:
    if v.count("/") != 1 or not all(v.split("/", 1)):
        raise ValueError(f"repo must be 'owner/name', got {v!r}")
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
    prefix: str = Field(min_length=1, description="Artifact-name prefix. Consumers name it in `[[deps]].package`.")
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
        "consumer by dispatch rather than `workflow_call`.",
    )
    submodules: Literal["true", "recursive"] | None = Field(
        default=None, description="`actions/checkout` `submodules` for the build jobs."
    )

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, v: str | None) -> str | None:
        return v if v is None else _check_repo(v)

    @field_validator("compiler_inputs")
    @classmethod
    def _compiler_inputs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_compiler_inputs(v)


class DepTable(_Table):
    """`[[deps]]`: one upstream package whose artifact every applicable leg fetches."""

    repo: str = Field(description="`owner/name` of the upstream repo.")
    package: str = Field(min_length=1, description="The upstream's `[package].prefix`.")
    ref: str = Field(
        description="Branch, tag or 40-char SHA. A sync branch of the same name in the upstream overrides it."
    )
    compiler_inputs: tuple[str, ...] = Field(
        alias="compiler-inputs",
        description="Leg fields holding the upstream's compilers; must match the upstream's "
        "`[package].compiler-inputs`. `[]` if the upstream is compiler-independent.",
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
    needs_python: StrictBool = Field(
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
        "values, compared as strings. A scalar is a one-element list. A scoped-out dep is absent from the leg's "
        "`cmake-prefix-path` and its deps hash.",
    )

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, v: str) -> str:
        return _check_repo(v)

    @field_validator("compiler_inputs")
    @classmethod
    def _compiler_inputs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_compiler_inputs(v)

    @field_validator("ref")
    @classmethod
    def _ref_nonempty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError('must be a branch, tag or 40-char SHA, e.g. "main"')
        return s

    @field_validator("options", mode="before")
    @classmethod
    def _option_scalar(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            raise ValueError(f"must be a scalar config name, not a list ({v!r}); name the combination (e.g. 'a-b')")
        if isinstance(v, str) and v and not _OPTION_TOKEN_RE.fullmatch(v):
            raise ValueError(f"invalid build option {v!r}: only [A-Za-z0-9_-] allowed")
        return v

    @field_validator("when", mode="before")
    @classmethod
    def _when_shape(cls, v: Any) -> Any:
        if v is None:
            return v
        if not isinstance(v, dict) or not v:
            raise ValueError(
                f'must be a non-empty table of matrix field to accepted value(s), e.g. {{ options = ["extended"] }}; '
                f"got {v!r}"
            )
        out: dict[str, tuple[str, ...]] = {}
        for key, accepted in v.items():
            values = accepted if isinstance(accepted, (list, tuple)) else [accepted]
            if not values:
                raise ValueError(f"when.{key} must list at least one accepted value")
            if any(isinstance(x, (dict, list, tuple)) for x in values):
                raise ValueError(f"when.{key} values must be scalars, got {accepted!r}")
            out[str(key)] = tuple(str(x) for x in values)
        return out


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
    the kind's action or job script. `platform` is required and names the
    binary-compatibility class; `build-type`, `python-version`, `options` and the
    `compiler-inputs` fields enter the artifact name; `runs-on` (which may name a
    runner class) and `container` only schedule the job.
    """

    triggers: tuple[str, ...] = Field(
        default=(),
        description="Events that run this kind in `cross-repo-trigger.yml`: `upstream-change`, `rebuild-request`. "
        "Empty keeps the kind out of it.",
    )
    needs: tuple[str, ...] = Field(
        default=(), description="Kinds that must finish first: `<kind>` locally, `<package-name>/<kind>` across repos."
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
        default=EXECUTION_RUNNER, description="`runner` (a GitHub Actions job) or `hpc` (a SLURM job)."
    )
    action: str = Field(
        default="",
        description="Runner kinds: the local composite the job calls, `./.github/actions/<name>`. Required with "
        "`triggers`.",
    )
    job_script: str = Field(
        default="",
        alias="job-script",
        description="HPC kinds: the recipe submitted to SLURM. Required with `triggers`.",
    )
    forwarded_inputs: tuple[str, ...] = Field(
        default=(), alias="forwarded-inputs", description="Leg fields passed to the action's `with:`."
    )
    forwarded_deps_outputs: tuple[str, ...] = Field(
        default=(),
        alias="forwarded-deps-outputs",
        description="`fetch-deps` outputs passed to the action's `with:`; only `cmake-prefix-path`.",
    )
    publishes: StrictBool = Field(
        default=True,
        description="Upload the install tree as an artifact. `false` for test kinds, whose action gets "
        "`own-artifact-name` instead.",
    )
    artifact_prefix: str | None = Field(
        default=None,
        alias="artifact-prefix",
        description="Publish under this prefix instead of `[package].prefix`, for a secondary artifact.",
    )
    container_credentials: StrictBool = Field(
        default=False,
        alias="container-credentials",
        description="Pull the leg's `container` with registry credentials.",
    )
    ctest: StrictBool = Field(
        default=False, description="Run ctest on the build tree before publishing; runner kinds only."
    )
    ctest_args: str = Field(default="", alias="ctest-args", description="Extra ctest arguments; needs `ctest = true`.")

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

    @field_validator("action")
    @classmethod
    def _action_path_shape(cls, v: str) -> str:
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


class DownstreamGateTable(_Table):
    """`[downstream-gate]`: when a pull request fans out to consumers."""

    label: str = Field(min_length=1, description="A PR fans out only while it carries this label; a push always does.")


class ManifestFile(_Table):
    """The whole of `.ci/manifest.toml`."""

    package: PackageTable
    deps: tuple[DepTable, ...] = ()
    trigger_downstream: tuple[TriggerDownstreamTable, ...] = Field(default=(), alias="trigger-downstream")
    downstream_gate: DownstreamGateTable | None = Field(default=None, alias="downstream-gate")
    generated: GeneratedTable | None = None
    matrix: dict[str, MatrixKindTable] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_duplicate_triggers(self) -> ManifestFile:
        seen: set[str] = set()
        for t in self.trigger_downstream:
            if t.repo in seen:
                raise ValueError(f"duplicate [[trigger-downstream]] for {t.repo!r}")
            seen.add(t.repo)
        return self


def validate(data: Mapping[str, Any]) -> ManifestFile:
    """Validate a loaded manifest; the first violation raises in TOML notation (`[matrix.build].ctest ...`)."""
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


#: How a pydantic loc head renders in TOML notation. `array` heads are arrays of
#: tables, so a numeric index becomes `[[deps]][2]`; `subtable` absorbs the next
#: element into the table name (`[matrix.build]`); `table` is a plain table.
_LOC_HEADS: Final = {
    "trigger-downstream": "array",
    "deps": "array",
    "matrix": "subtable",
    "package": "table",
    "generated": "table",
    "downstream-gate": "table",
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
