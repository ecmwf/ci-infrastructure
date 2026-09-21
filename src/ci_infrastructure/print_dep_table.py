#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Render the OWN package and its resolved deps as a Markdown table in $GITHUB_STEP_SUMMARY.

The input is one leg's whole `_resolved` block, so the key mapping lives here once::

  --resolved '{own-name, own-ref, own-sha, own-platform, own-compiler,
               own-build-type, own-python, own-deps-hash,
               deps: [{name, repo, ref, sha, platform, compiler, build-type,
                       python-version, deps-hash, source, ...}, ...], ...}'

Usage::

    print_dep_table.py --resolved '<JSON>' [--own-repo o/r] [--own-source built]
                       [--title "..."]
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, TypedDict

import click

from ._errors import CIError

# A ref that is itself a commit SHA; the Ref column blanks it.
_SHA_RE: Final = re.compile(r"[0-9a-f]{7,40}")


class Row(TypedDict, total=False):
    package: str
    ref: str
    sha: str
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


#: `_resolved` own-* key -> the dep key `_row_from_dep` reads.
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
    """The OWN row from `_resolved`'s own-* fields; `source` is what the job did, not the resolver."""
    row = {column: resolved.get(key, "") for key, column in _OWN_COLUMNS.items()}
    row["repo"] = repo
    row["source"] = source
    return row


def parse_resolved(resolved_json: str) -> Mapping[str, Any]:
    """Parse and vet `--resolved`, failing loudly rather than rendering blank."""
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

    # deps are upstream-first; list them nearest-first below the OWN row.
    for dep in reversed(deps):
        if isinstance(dep, dict):
            rows.append(_row_from_dep(dep))

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
