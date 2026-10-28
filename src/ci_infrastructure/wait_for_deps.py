#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Wait in one small job for the deps of all legs of a resolved matrix (used by `wait-for-deps`).

Otherwise each leg waits in fetch-deps on its own runner, and a downstream fan-out holds the
runners its upstream needs to build. Never fails: a leg's fetch-deps reports what is missing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import click

from ._github_api import WorkflowRuns, probe_workflow_runs, select_token
from .fetch_deps import _run_phase, poll_for_artifact


def pending(matrix: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(repo, sha, artifact name) of each dep not cached at resolve time, each once."""
    seen: dict[str, tuple[str, str, str]] = {}
    for leg in matrix.get("include") or []:
        for dep in (leg.get("_resolved") or {}).get("deps") or []:
            if not dep.get("cached"):
                seen.setdefault(dep["artifact-name"], (dep["repo"], dep["sha"], dep["artifact-name"]))
    return list(seen.values())


def by_upstream(deps: list[tuple[str, str, str]]) -> dict[tuple[str, str], list[str]]:
    """Artifact names per (repo, sha), in first-seen order."""
    groups: dict[tuple[str, str], list[str]] = {}
    for repo, sha, name in deps:
        groups.setdefault((repo, sha), []).append(name)
    return groups


def _run_status(run: WorkflowRuns) -> str:
    if run.state == "running":
        return _run_phase(run.detail)
    if run.state == "completed":
        return f"completed ({run.conclusion})"
    return "no run"


def table(groups: dict[tuple[str, str], list[str]], probe: Callable[[str, str], WorkflowRuns]) -> list[str]:
    """One row per upstream run: what is waited for, the run's state and link."""
    rows = [("upstream", "artifacts", "run", "")]
    for (repo, sha), names in groups.items():
        run = probe(repo, sha)
        rows.append((f"{repo}@{sha[:8]}", str(len(names)), _run_status(run), run.url or ""))
    widths = [max(len(r[i]) for r in rows) for i in range(3)] + [0]
    return ["  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]


@click.command(help="Wait until the uncached deps of a resolved matrix are in the S3 store.")
@click.option("--matrix-json", "matrix_json", required=True, help='A resolve-deps matrix, {"include": [...]}')
def main(matrix_json: str) -> None:
    deps = pending(json.loads(matrix_json or "{}"))
    if not deps:
        print("wait_for_deps: every dep was cached at resolve time; nothing to wait for.")
        return
    token = select_token()
    groups = by_upstream(deps)
    print(f"wait_for_deps: waiting on {len(deps)} artifact(s) from {len(groups)} upstream run(s):")
    print("\n".join(table(groups, lambda repo, sha: probe_workflow_runs(repo, sha, token))))
    missing: list[str] = []
    for (repo, sha), names in groups.items():
        lost = [name for name in names if not poll_for_artifact(repo, sha, name, token)]
        missing += lost
        done = len(names) - len(lost)
        print(f"wait_for_deps: {repo}@{sha[:8]}: {done}/{len(names)} artifact(s) in the store")
    if missing:
        print(
            f"::warning::Still not in the store: {', '.join(missing)}. "
            "fetch-deps checks again in the legs that need them.",
            file=sys.stderr,
        )
    else:
        print("wait_for_deps: all deps are in the store.")


if __name__ == "__main__":
    main()
