# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Shared utilities for the ci-infrastructure CLI scripts.

Two clusters live here:

  * GitHub-API helpers — every script shells out to the `gh` CLI rather than
    maintaining HTTP sessions; runners always ship `gh` and it picks up
    `GH_TOKEN` from the environment.
  * Artifact naming — `make_artifact_name` and the primitives it composes
    (`compute_platform_slug`, `compute_deps_hash8`, `canonical_option_segment`).
    `resolve_deps` mints names and `check_artifact` re-derives them to look
    artifacts up; a byte of disagreement makes every cache lookup miss, so
    there is exactly one definition and both import it.

Type-wise the module stays in plain `str`: callers with stricter NewTypes
(`Repo`, `Ref`, `Sha`, `ArtifactName` in `resolve_deps`) pass them through
unchanged thanks to NewType subtype compatibility.
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

# Restrict to GitHub's allowed alias chars.
_ALIAS_SAFE_RE: Final = re.compile(r"[^A-Za-z0-9_]")

# GitHub Actions run.status values that mean "not yet done". Used to gate
# polling loops in fetch_deps and the in-flight check in resolve_deps.
IN_PROGRESS_STATUSES: Final = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

_HEX_CHARS: Final = frozenset("0123456789abcdef")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")


def _alias(s: str) -> str:
    """Make a string safe for GraphQL field aliases."""
    return _ALIAS_SAFE_RE.sub("_", s)


def _gh(args: Sequence[str], token: str | None, input_text: str | None = None) -> tuple[int, str, str]:
    """Run a `gh` subcommand and return (rc, stdout, stderr).

    `token` precedence mirrors gh's own: a non-None value is exported as
    GH_TOKEN, overriding whatever was in the parent shell. None leaves the
    parent env untouched, so gh falls back to (in order) GITHUB_TOKEN, then
    its on-disk keychain auth from `gh auth login`.
    """
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
        # Surface the failure so the resolve job logs show why a lookup returned None
        # (the most common culprit is a token without actions:read on the upstream repo).
        print(f"::warning::REST call failed for {path}: {err.strip()}", file=sys.stderr)
        return None
    return cast(JSON, json.loads(out))


def gh_api_graphql(query: str, token: str | None) -> JSON | None:
    """Execute a GraphQL query via 'gh api graphql -f query=…'."""
    rc, out, err = _gh(["gh", "api", "graphql", "-f", f"query={query}"], token)
    if rc != 0:
        print(f"::warning::GraphQL call failed: {err.strip()}", file=sys.stderr)
        return None
    parsed = cast(JSON, json.loads(out))
    if isinstance(parsed, dict) and parsed.get("errors"):
        print(f"::warning::GraphQL returned errors: {parsed['errors']}", file=sys.stderr)
    return parsed


def select_token() -> str | None:
    """Return the first non-empty token from the standard env var precedence,
    or None if none is set. Callers pass the result straight to `_gh`, which
    treats None as "let gh fall back to keychain auth".
    """
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
    """Fetch a layer of (repo, ref) manifests in one GraphQL call. Also checks
    whether `sync_branch` exists in each repo (if sync_branch is non-None).

    Returns mapping (repo, ref) -> (manifest_text_or_None, sync_branch_exists).
    """
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
    """First 8 chars of SHA-256 of sorted, space-separated dep artifact names.

    Returns None when there are no deps to hash — the caller threads that through
    to make_artifact_name where it elides the deps-hash segment of the name.
    """
    cleaned = sorted(n for n in dep_artifact_names if n)
    if not cleaned:
        return None
    return hashlib.sha256(" ".join(cleaned).encode()).hexdigest()[:8]


_OPTION_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_option_segment(option: str) -> str:
    """The artifact-name segment for a build-option config, or '' when empty.

    Options are an orthogonal build axis: a scalar named configuration (e.g.
    'stochastic-moments', or a curated combo name like 'moments-fast') that varies
    independently of build-type. The segment is the config name behind an 'opts.'
    marker so it can never be confused with the free-form <build-type> segment that
    precedes it. Empty option -> '' so a plain build's artifact name is
    byte-identical to the pre-options format (no cache churn). Shared by resolve_deps
    (mints names) and check_artifact (mirrors them) so the two can't drift.

    Raises ValueError on a name outside [A-Za-z0-9_-] (would corrupt the segment
    separators).
    """
    if not option:
        return ""
    if not _OPTION_TOKEN_RE.fullmatch(option):
        raise ValueError(f"invalid build option {option!r}: only [A-Za-z0-9_-] allowed")
    return "opts." + option


def compute_platform_slug(platform: str) -> str:
    """Return the artifact-name platform slot from the required, explicit
    `platform` declaration (used verbatim).

    `platform` names the binary-compatibility class. Several images that are
    ABI-compatible — plus a host-mode GH runner on the same distro — declare
    the same `platform` and so share one artifact: an `ubuntu24.04-gfortran13`
    image and an `ubuntu24.04-clang18-gfortran13` image both declare
    `platform = "ubuntu-24.04"`, so a fortmath built under the first is reused
    by a cxxmath build under the second instead of being rebuilt just because
    the image tag changed.

    Because the image tag does not enter the slug, the caller owns cache
    invalidation: bump the platform string (e.g. `ubuntu-24.04-r2`) when an
    ABI-relevant change ships within one distro release.

    Raises ValueError when `platform` is empty (it is required) or when the
    slug begins with an 8-hex-char segment (would collide with the deps-hash8
    slot).
    """
    slug = platform.strip()
    if not slug:
        raise ValueError("platform is required: every matrix leg must declare a 'platform' (e.g. ubuntu-24.04).")
    # Disambiguation with the optional deps-hash8 segment: the artifact-name parser
    # only confuses a slug whose first hyphen-separated segment is exactly 8 hex
    # chars. Single-char-hex starts like `arc-sandbox-cci2` are fine because
    # the first segment (`arc`) is too short to be a hash.
    first = slug.split("-", 1)[0].lower()
    if len(first) == 8 and all(c in _HEX_CHARS for c in first):
        raise ValueError(
            f"Platform slug '{slug}' begins with an 8-hex-char segment, which would "
            "collide with the deps-hash8 segment in artifact names."
        )
    return slug


def resolve_ref_to_sha(repo: str, ref: str, token: str | None) -> str:
    """Resolve a branch / lightweight or annotated tag / short or full SHA to a 40-char commit SHA.

    Uses the commits/{ref} REST endpoint, which auto-disambiguates and dereferences
    annotated tags to the underlying commit — the tags/{ref} endpoint returns the
    tag-object SHA for an annotated tag, which is not what artifacts are keyed on.
    """
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
) -> str:
    """The single definition of an artifact's name.

        <prefix>-<sha>[-<deps-hash8>]-<platform>[-<compiler>][-py<ver>]-<build-type>[-opts.<name>]

    Shared by resolve_deps (which mints names) and check_artifact (which re-derives
    them to look one up); the two must agree byte-for-byte or every cache lookup
    misses. None for deps_hash8 / compiler / python_version means "this segment
    does not apply" (no deps, no compilers declared, not a Python build) and the
    segment is dropped. An empty `option` appends nothing.
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
    return "-".join(parts)


#: Run conclusions that mean "this run did not succeed". `cancelled` counts:
#: a cancelled producer never published, so a consumer must not keep waiting.
_FAILURE_CONCLUSIONS: Final = frozenset({"failure", "cancelled", "timed_out", "action_required", "startup_failure"})


class WorkflowRuns(NamedTuple):
    """What the workflow runs for one commit SHA say about it.

    state       'running' (>=1 run queued/in-progress), 'completed' (all done),
                or 'none' (no runs for this SHA).
    detail      concrete GitHub status of the first in-progress run, e.g.
                'queued' or 'in_progress'; None unless state == 'running'.
    url         that run's html_url; None unless state == 'running'.
    conclusion  'failure' if any completed run failed/was cancelled, else
                'success'; None unless state == 'completed'.

    One probe serves three questions: whether to keep waiting (fetch_deps),
    whether a missing artifact is explained by a failed build (check_artifact),
    and whether a producer is already building (resolve_deps).
    """

    state: Literal["running", "completed", "none"]
    detail: str | None = None
    url: str | None = None
    conclusion: Literal["success", "failure"] | None = None

    @property
    def in_flight(self) -> bool:
        return self.state == "running"


def probe_workflow_runs(repo: str, sha: str, token: str | None) -> WorkflowRuns:
    """Probe the workflow runs GitHub has for `sha`; see WorkflowRuns.

    An in-progress run wins over a failed one: while anything is still going the
    outcome is not decided yet.
    """
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
    """Append `key=value` lines to $GITHUB_OUTPUT, or print them when unset.

    Actions outputs are stringly-typed, so None collapses to the empty string
    here — at the wire boundary — rather than being carried as a "" sentinel
    through the callers' own types. A value containing a newline needs GitHub's
    delimited form; the delimiter is content-derived so it cannot appear inside
    the value it terminates.
    """
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
    """The include legs `[matrix.<kind>]` ends up with after `reuse-matrix`.

    `reuse-matrix = "X"` means "share X's legs" — a test kind almost always wants
    the exact matrix its build kind published for, and repeating the legs is how
    they drift apart. Both the generator and the resolver expand this, and they
    must agree: a consumer whose test legs differed from its build legs would
    look up artifact names nothing ever published.

    Chained reuse is refused rather than followed, so the legs of a kind are
    always one hop from a literal `include`.
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
        legs = target.get("include") or ()
    if not isinstance(legs, (list, tuple)):
        raise ManifestSchemaError(f"[matrix.{kind}.include] must be an array of tables")
    return tuple(dict(leg) for leg in legs)
