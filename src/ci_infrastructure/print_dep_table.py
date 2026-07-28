#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
print_dep_table.py

Renders a Markdown table of the OWN package + each resolved dep to
$GITHUB_STEP_SUMMARY so CI job logs show a human-readable dependency
overview with clickable links to each upstream repo and commit.

Inputs are the JSON the resolver already produces:

  --deps-json '[{name, repo, ref, sha, artifact-name, platform, compiler,
                 build-type, python-version, deps-hash, source, ...}, ...]'
  --own       '{name, repo, ref, sha, artifact-name, platform, compiler,
                 build-type, python-version, deps-hash, source}'   (optional)

The resolver carries the structured fields (platform / compiler / python /
build-type / deps-hash) explicitly, so each column is read straight from the
dep dict — no parsing of the artifact name. The Package column links to the
upstream repo, the SHA column links to the upstream commit, and the Ref column
shows the resolved branch/tag (blank when a literal SHA was pinned).

Usage:
    print_dep_table.py --deps-json '<JSON>' [--own '<JSON>'] [--title "..."]
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


@click.command(help="Print artifact dependency table to step summary.")
@click.option(
    "--deps-json",
    "deps_json",
    default="[]",
    help='JSON array of resolved dep dicts (resolver _resolved.deps), default "[]"',
)
@click.option(
    "--own",
    default="",
    help="JSON object with the OWN package row {name, repo, sha, artifact-name, source}",
)
@click.option("--title", default="Resolved dependencies", help="Table heading")
def main(deps_json: str, own: str, title: str) -> None:
    deps = json.loads(deps_json)
    if not isinstance(deps, list):
        raise CIError("--deps-json must be a JSON array")

    rows: list[Row] = []

    if own.strip():
        own_row = json.loads(own)
        if isinstance(own_row, dict):
            rows.append(_row_from_dep(own_row))

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
