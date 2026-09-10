#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Renders a Markdown table of the OWN package + each resolved dep to
$GITHUB_STEP_SUMMARY so CI job logs show a human-readable dependency
overview with clickable links to each upstream repo and commit.

The input is the resolver's `_resolved` block for one matrix leg, whole:

  --resolved '{own-name, own-ref, own-sha, own-platform, own-compiler,
               own-build-type, own-python, own-deps-hash,
               deps: [{name, repo, ref, sha, platform, compiler, build-type,
                       python-version, deps-hash, source, ...}, ...], ...}'

Whole, rather than a field list, because a workflow spelling out
`own-sha: ${{ matrix._resolved.own-sha }}` ten times is ten chances to mistype a
key into a silently blank column -- which is how three repos came to render an
empty table for months. The key mapping lives here, once, where it is tested.

The resolver carries the structured fields (platform / compiler / python /
build-type / deps-hash) explicitly, so each column is read straight from the
dep dict — no parsing of the artifact name. The Package column links to the
upstream repo, the SHA column links to the upstream commit, and the Ref column
shows the resolved branch/tag (blank when a literal SHA was pinned).

Usage:
    print_dep_table.py --resolved '<JSON>' [--own-repo o/r] [--own-source built]
                       [--title "..."]
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

import click

from ._errors import CIError

# A ref that is itself a (full or abbreviated) commit SHA — a literal pin. The
# Ref column blanks these out, since the SHA column already shows the commit.
_SHA_RE: Final = re.compile(r"[0-9a-f]{7,40}")


class Row(TypedDict, total=False):
    package: str  # markdown link to repo
    ref: str  # branch/tag name (blank when a SHA was pinned)
    sha: str  # markdown link to commit
    deps_hash: str
    platform: str
    compiler: str
    python: str
    build_type: str
    source: str


def _commit_url(repo: str, sha: str) -> str:
    return f"https://github.com/{repo}/commit/{sha}"


def _repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _looks_like_sha(ref: str) -> bool:
    return bool(_SHA_RE.fullmatch(ref))


def _row_from_dep(dep: Mapping[str, Any]) -> Row:
    """Build a Row from a resolver dep dict, reading each column directly."""
    name = str(dep.get("name", ""))
    repo = str(dep.get("repo", ""))
    sha = str(dep.get("sha", ""))
    ref = str(dep.get("ref", ""))

    short_sha = sha[:8] if sha else ""
    return Row(
        package=f"[{name}]({_repo_url(repo)})" if repo else name,
        ref="" if _looks_like_sha(ref) else ref,
        sha=f"[`{short_sha}`]({_commit_url(repo, sha)})" if repo and sha else short_sha,
        deps_hash=str(dep.get("deps-hash", "")),
        platform=str(dep.get("platform", "")),
        compiler=str(dep.get("compiler", "")),
        python=str(dep.get("python-version", "")),
        build_type=str(dep.get("build-type", "")),
        source=str(dep.get("source", "")),
    )


def _md_table(rows: Sequence[Row], show_source: bool) -> str:
    headers = ["Package", "Ref", "SHA", "Deps hash", "Platform", "Compiler", "Python", "Build type"]
    keys: list[str] = ["package", "ref", "sha", "deps_hash", "platform", "compiler", "python", "build_type"]
    if show_source:
        headers.append("Source")
        keys.append("source")

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, k in enumerate(keys):
            col_widths[i] = max(col_widths[i], len(str(row.get(k, ""))))

    def fmt(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, col_widths)) + " |"

    lines = [
        fmt(headers),
        "| " + " | ".join("-" * w for w in col_widths) + " |",
        *[fmt([str(row.get(k, "")) for k in keys]) for row in rows],
    ]
    return "\n".join(lines)


#: `_resolved` own-* key -> the un-prefixed key `_row_from_dep` reads. Only
#: own-python needs saying twice; the rest are the same word without the prefix.
_OWN_COLUMNS: Final = {
    "own-name": "name",
    "own-ref": "ref",
    "own-sha": "sha",
    "own-platform": "platform",
    "own-compiler": "compiler",
    "own-build-type": "build-type",
    "own-python": "python-version",
    "own-deps-hash": "deps-hash",
}


def own_row_from_resolved(resolved: Mapping[str, Any], repo: str, source: str) -> dict[str, Any]:
    """The OWN row, projected out of `_resolved`'s own-* fields.

    `repo` and `source` are not in `_resolved` and cannot be: the first is the
    workflow's own repository, the second is what the JOB did (built it, or found
    it already published) rather than what the resolver decided.
    """
    row = {column: resolved.get(key, "") for key, column in _OWN_COLUMNS.items()}
    row["repo"] = repo
    row["source"] = source
    return row


def parse_resolved(resolved_json: str) -> Mapping[str, Any]:
    """Parse and vet `--resolved`, failing loudly rather than rendering blank.

    Every check here is a shape a caller can actually produce: an input name the
    action does not declare arrives as the empty string (GitHub only warns about
    an unknown input), and a hand-rolled JSON blob is the other way in.
    """
    if not resolved_json.strip():
        raise CIError("--resolved is required and was empty; pass the leg's `matrix._resolved` as JSON")
    try:
        resolved = json.loads(resolved_json)
    except json.JSONDecodeError as exc:
        raise CIError(f"--resolved is not valid JSON: {exc}") from exc
    if not isinstance(resolved, dict):
        raise CIError(f"--resolved must be a JSON object (the _resolved block), got {type(resolved).__name__}")
    if "deps" not in resolved:
        raise CIError("--resolved has no 'deps' key; this is not a _resolved block")
    if not isinstance(resolved["deps"], list):
        raise CIError(f"--resolved.deps must be an array, got {type(resolved['deps']).__name__}")
    if not resolved.get("own-name"):
        # resolve-deps and this action are both pinned @main and run in the same
        # job graph, so they cannot legitimately disagree about the schema.
        raise CIError(
            "--resolved has no 'own-name'; the matrix was produced by a resolve-deps "
            "older than this action. Re-run the workflow so both come from the same ref."
        )
    return resolved


@click.command(help="Print artifact dependency table to step summary.")
@click.option(
    "--resolved",
    "resolved_json",
    default="",
    help="The leg's _resolved block as JSON (matrix._resolved)",
)
@click.option("--own-repo", default="", help="owner/repo of the OWN package; empty renders its name unlinked")
@click.option("--own-source", default="built", help="How this job got the OWN artifact: built / artifact")
@click.option("--title", default="Resolved dependencies", help="Table heading")
def main(resolved_json: str, own_repo: str, own_source: str, title: str) -> None:
    resolved = parse_resolved(resolved_json)
    deps = resolved["deps"]

    rows: list[Row] = [_row_from_dep(own_row_from_resolved(resolved, own_repo, own_source))]

    # The deps array is ordered upstream→downstream (the link order the build
    # needs). The table reads top-down from the OWN package, so list deps
    # nearest-first: reverse to downstream→upstream below the OWN row.
    for dep in reversed(deps):
        if isinstance(dep, dict):
            rows.append(_row_from_dep(dep))

    if not rows:
        print("No artifact names to display.", file=sys.stderr)
        return

    show_source = any(row.get("source") for row in rows)
    table = f"## {title}\n\n{_md_table(rows, show_source)}\n"

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(table)
    else:
        print(table)


if __name__ == "__main__":
    main()
