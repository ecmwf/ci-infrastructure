"""Shared utilities for the ci-infrastructure CLI scripts.

Two clusters live here:

  * GitHub-API helpers — every script shells out to the `gh` CLI rather than
    maintaining HTTP sessions; runners always ship `gh` and it picks up
    `GH_TOKEN` from the environment.
  * Artifact-naming primitives (`compute_platform_slug`, `compute_deps_hash8`)
    shared by `resolve_deps` (which mints names) and `check_artifact` (which
    re-derives them to look up artifacts). Keeping both in one place avoids
    the "Mirrored from X" drift the two used to acknowledge in comments.

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
from collections.abc import Sequence
from typing import Any, Final, TypeAlias, cast

JSON: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None

# Restrict to GitHub's allowed alias chars; we use this when aliasing fields in
# GraphQL queries to avoid quoting issues.
_ALIAS_SAFE_RE: Final = re.compile(r"[^A-Za-z0-9_]")

# GitHub Actions run.status values that mean "not yet done". Used to gate
# polling loops in fetch_deps and the in-flight check in resolve_deps.
IN_PROGRESS_STATUSES: Final = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

_HEX_CHARS: Final = frozenset("0123456789abcdef")


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


def fetch_repo_head(repo: str, token: str | None) -> tuple[str, str] | None:
    """Return (default_branch_name, head_commit_sha) for `repo`, or None if
    the lookup fails (missing repo, network blip, auth). The caller uses this
    to detect drift between the local working tree and what GitHub serves at
    the default branch tip — silent return-None on failure lets the caller
    emit a soft "couldn't verify" warning rather than crashing.
    """
    owner, name = repo.split("/", 1)
    query = (
        "query {\n"
        f'  repository(owner: "{owner}", name: "{name}") {{\n'
        "    defaultBranchRef { name target { ... on Commit { oid } } }\n"
        "  }\n"
        "}"
    )
    data = gh_api_graphql(query, token)
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    repo_node = payload.get("repository")
    if not isinstance(repo_node, dict):
        return None
    ref_node = repo_node.get("defaultBranchRef")
    if not isinstance(ref_node, dict):
        return None
    name_val = ref_node.get("name")
    target = ref_node.get("target")
    if not isinstance(name_val, str) or not isinstance(target, dict):
        return None
    oid = target.get("oid")
    if not isinstance(oid, str):
        return None
    return name_val, oid


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
        # Failed entirely — mark all as missing.
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

    Because the image tag no longer enters the slug, the caller owns cache
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
