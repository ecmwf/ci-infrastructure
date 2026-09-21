# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers: GitHub API calls via the `gh` CLI, and artifact naming.

Artifact naming lives only here: `resolve_deps` mints names and `check_artifact`
re-derives them, and any disagreement misses every cache lookup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, NamedTuple, TypeAlias, cast

from ._errors import CIError

JSON: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None

# How a kind's build runs: on a GitHub runner, or as a SLURM job via build-on-hpc.
Execution: TypeAlias = Literal["runner", "hpc"]
EXECUTION_RUNNER: Final[Execution] = "runner"
EXECUTION_HPC: Final[Execution] = "hpc"

#: Version of the shared HPC base template (hpc/templates/cmake-build.sh.j2), carried
#: in every hpc artifact name. Consumers load ci-infrastructure @main, so a template
#: change reaches every repo without moving any sha and would otherwise be served
#: from cache. 0 adds no segment.
HPC_TEMPLATE_VERSION: Final = 0


def template_version_for_lane(lane: Execution) -> int:
    return HPC_TEMPLATE_VERSION if lane == EXECUTION_HPC else 0


def lane_suffix(lane: Execution) -> str:
    """Generated-workflow filename suffix: '' for the runner lane, '-hpc' for hpc."""
    return "" if lane == EXECUTION_RUNNER else "-hpc"


_ALIAS_SAFE_RE: Final = re.compile(r"[^A-Za-z0-9_]")

IN_PROGRESS_STATUSES: Final = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

_HEX_CHARS: Final = frozenset("0123456789abcdef")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")


def _alias(s: str) -> str:
    """Make a string safe for GraphQL field aliases."""
    return _ALIAS_SAFE_RE.sub("_", s)


def _gh(args: Sequence[str], token: str | None, input_text: str | None = None) -> tuple[int, str, str]:
    """Run `gh`; a token overrides GH_TOKEN, None leaves gh's own auth fallback."""
    env = {**os.environ}
    if token:
        env["GH_TOKEN"] = token
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def gh_api_rest(path: str, token: str | None) -> JSON | None:
    rc, out, err = _gh(["gh", "api", path], token)
    if rc != 0:
        print(f"::warning::REST call failed for {path}: {err.strip()}", file=sys.stderr)
        return None
    return cast(JSON, json.loads(out))


def gh_api_graphql(query: str, token: str | None) -> JSON | None:

    rc, out, err = _gh(["gh", "api", "graphql", "-f", f"query={query}"], token)
    if rc != 0:
        print(f"::warning::GraphQL call failed: {err.strip()}", file=sys.stderr)
        return None
    parsed = cast(JSON, json.loads(out))
    if isinstance(parsed, dict) and parsed.get("errors"):
        print(f"::warning::GraphQL returned errors: {parsed['errors']}", file=sys.stderr)
    return parsed


def select_token() -> str | None:
    """First non-empty of GH_TOKEN, ORG_READ_TOKEN, GITHUB_TOKEN, else None."""
    for var in ("GH_TOKEN", "ORG_READ_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def fetch_manifests_layer(
    repos_refs: Sequence[tuple[str, str]],
    sync_branch: str | None,
    token: str | None,
    manifest_path: str,
) -> dict[tuple[str, str], tuple[str | None, bool]]:
    """One GraphQL call: (repo, ref) -> (manifest text or None, whether sync_branch exists)."""
    if not repos_refs:
        return {}

    selections: list[str] = []
    aliases: dict[str, tuple[str, str]] = {}
    for repo, ref in repos_refs:
        owner, name = repo.split("/", 1)
        a_man = f"m_{_alias(repo)}_{_alias(ref)}"
        selections.append(
            f'  {a_man}: repository(owner: "{owner}", name: "{name}") {{\n'
            f'    object(expression: "{ref}:{manifest_path}") {{\n'
            f"      ... on Blob {{ text }}\n"
            f"    }}\n"
            f"  }}"
        )
        aliases[a_man] = (repo, ref)

        if sync_branch:
            a_sync = f"s_{_alias(repo)}"
            selections.append(
                f'  {a_sync}: repository(owner: "{owner}", name: "{name}") {{\n'
                f'    ref(qualifiedName: "refs/heads/{sync_branch}") {{ name }}\n'
                f"  }}"
            )

    query = "query {\n" + "\n".join(selections) + "\n}"
    data = gh_api_graphql(query, token)
    out: dict[tuple[str, str], tuple[str | None, bool]] = {}
    if not isinstance(data, dict) or "data" not in data or not isinstance(data["data"], dict):
        for repo, ref in repos_refs:
            out[(repo, ref)] = (None, False)
        return out

    payload = data["data"]
    for a_man, (repo, ref) in aliases.items():
        node = payload.get(a_man)
        text: str | None = None
        if isinstance(node, dict):
            obj = node.get("object")
            if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                text = obj["text"]
        sync_exists = False
        if sync_branch:
            sync_node = payload.get(f"s_{_alias(repo)}")
            if isinstance(sync_node, dict) and isinstance(sync_node.get("ref"), dict):
                sync_exists = True
        out[(repo, ref)] = (text, sync_exists)
    return out


def compute_deps_hash8(dep_artifact_names: Sequence[str]) -> str | None:
    """First 8 hex chars of SHA-256 over the sorted dep artifact names; None without deps."""
    cleaned = sorted(n for n in dep_artifact_names if n)
    if not cleaned:
        return None
    return hashlib.sha256(" ".join(cleaned).encode()).hexdigest()[:8]


_OPTION_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_option_segment(option: str) -> str:
    """'opts.<option>' ('' when empty); the marker keeps it apart from the free-form build-type."""
    if not option:
        return ""
    if not _OPTION_TOKEN_RE.fullmatch(option):
        raise ValueError(f"invalid build option {option!r}: only [A-Za-z0-9_-] allowed")
    return "opts." + option


def compute_platform_slug(platform: str) -> str:
    """The required `platform` (a binary-compatibility class), verbatim, as the artifact-name slot.

    ABI-compatible images declaring the same platform share artifacts. Raises when
    empty, or when the first segment is 8 hex chars (would read as deps-hash8).
    """
    slug = platform.strip()
    if not slug:
        raise ValueError("platform is required: every matrix leg must declare a 'platform' (e.g. ubuntu-24.04).")
    first = slug.split("-", 1)[0].lower()
    if len(first) == 8 and all(c in _HEX_CHARS for c in first):
        raise ValueError(
            f"Platform slug '{slug}' begins with an 8-hex-char segment, which would "
            "collide with the deps-hash8 segment in artifact names."
        )
    return slug


def resolve_ref_to_sha(repo: str, ref: str, token: str | None) -> str:
    """Resolve a branch / tag / short or full SHA to a 40-char commit SHA (annotated tags dereferenced)."""
    if _SHA_RE.fullmatch(ref):
        return ref
    data = gh_api_rest(f"repos/{repo}/commits/{ref}", token)
    if isinstance(data, dict) and isinstance(data.get("sha"), str):
        sha = cast(str, data["sha"])
        if not _SHA_RE.fullmatch(sha):
            raise CIError(f"GitHub returned a malformed commit SHA for {repo}@{ref}: {sha!r}")
        return sha
    raise CIError(f"Could not resolve ref {ref!r} in {repo}")


def make_artifact_name(
    prefix: str,
    sha: str,
    deps_hash8: str | None,
    platform_slug: str,
    compiler: str | None,
    build_type: str,
    python_version: str | None,
    option: str = "",
    *,
    template_version: int = 0,
) -> str:
    """The single definition of an artifact's name.

        <prefix>-<sha>[-<deps-hash8>]-<platform>[-<compiler>][-py<ver>]-<build-type>[-hpcv<N>][-opts.<name>]

    None / empty / zero drops the optional segment.
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
    if template_version:
        parts.append(f"hpcv{template_version}")
    opt_seg = canonical_option_segment(option)
    if opt_seg:
        parts.append(opt_seg)
    return "-".join(parts)


#: Run conclusions that mean "this run did not succeed". `cancelled` counts:
#: a cancelled producer never published, so a consumer must not keep waiting.
_FAILURE_CONCLUSIONS: Final = frozenset({"failure", "cancelled", "timed_out", "action_required", "startup_failure"})


class WorkflowRuns(NamedTuple):
    """What the workflow runs for one commit SHA say.

    detail and url describe the first in-progress run (state 'running' only);
    conclusion is set only for 'completed'.
    """

    state: Literal["running", "completed", "none"]
    detail: str | None = None
    url: str | None = None
    conclusion: Literal["success", "failure"] | None = None

    @property
    def in_flight(self) -> bool:
        return self.state == "running"


def probe_workflow_runs(repo: str, sha: str, token: str | None) -> WorkflowRuns:
    """Probe the workflow runs for `sha`; an in-progress run wins over a failed one."""
    data = gh_api_rest(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100", token)
    if not isinstance(data, dict):
        return WorkflowRuns("none")
    runs = [r for r in data.get("workflow_runs") or [] if isinstance(r, dict)]
    if not runs:
        return WorkflowRuns("none")
    for run in runs:
        if run.get("status") in IN_PROGRESS_STATUSES:
            status, html_url = run.get("status"), run.get("html_url")
            return WorkflowRuns(
                "running",
                status if isinstance(status, str) else None,
                html_url if isinstance(html_url, str) else None,
            )
    if any(run.get("conclusion") in _FAILURE_CONCLUSIONS for run in runs):
        return WorkflowRuns("completed", conclusion="failure")
    return WorkflowRuns("completed", conclusion="success")


def write_outputs(outputs: Mapping[str, object]) -> None:
    """Append `key=value` lines to $GITHUB_OUTPUT (or print them); None becomes "", multi-line values are delimited."""
    lines: list[str] = []
    for key, raw in outputs.items():
        value = "" if raw is None else str(raw)
        if "\n" in value:
            delim = f"EOF_{hashlib.sha1(value.encode()).hexdigest()[:8]}"
            lines.append(f"{key}<<{delim}\n{value}\n{delim}")
        else:
            lines.append(f"{key}={value}")
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as fh:
            fh.write("".join(f"{line}\n" for line in lines))
    else:
        for line in lines:
            print(line)


class ManifestSchemaError(Exception):
    """A manifest violates the shared matrix schema."""


def resolve_reuse_matrix(
    kind: str, include: Sequence[Any] | None, reuse: object, blocks: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """The legs of `[matrix.<kind>]` after `reuse-matrix = "X"` (share X's legs; no chaining).

    Each leg is laid over its matrix's `defaults` table; a reusing kind's own
    `defaults` then go under X's. Shared by the generator and the resolver,
    which must agree on every leg.
    """
    if reuse is not None and include:
        raise ManifestSchemaError(f"[matrix.{kind}] sets both 'reuse-matrix' and 'include'; pick one")
    if reuse is None:
        legs = include or ()
    else:
        target = blocks.get(str(reuse))
        if target is None:
            raise ManifestSchemaError(
                f"[matrix.{kind}].reuse-matrix = {str(reuse)!r} but [matrix.{reuse}] does not exist"
            )
        if target.get("reuse-matrix") is not None:
            raise ManifestSchemaError(
                f"[matrix.{kind}].reuse-matrix = {str(reuse)!r} is itself a reuse-matrix; "
                f"chained reuse is not supported"
            )
        legs = _with_defaults(str(reuse), target.get("include") or (), target.get("defaults"))
    own = blocks.get(kind, {}).get("defaults")
    return _with_defaults(kind, legs, own)


def _with_defaults(kind: str, legs: object, defaults: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(legs, (list, tuple)):
        raise ManifestSchemaError(f"[matrix.{kind}.include] must be an array of tables")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, Mapping):
        raise ManifestSchemaError(f"[matrix.{kind}.defaults] must be a table")
    return tuple({**defaults, **leg} for leg in legs)
