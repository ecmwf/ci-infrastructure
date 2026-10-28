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
from typing import Any

import click

from ._github_api import select_token
from .fetch_deps import poll_for_artifact


def pending(matrix: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(repo, sha, artifact name) of each dep not cached at resolve time, each once."""
    seen: dict[str, tuple[str, str, str]] = {}
    for leg in matrix.get("include") or []:
        for dep in (leg.get("_resolved") or {}).get("deps") or []:
            if not dep.get("cached"):
                seen.setdefault(dep["artifact-name"], (dep["repo"], dep["sha"], dep["artifact-name"]))
    return list(seen.values())


@click.command(help="Wait until the uncached deps of a resolved matrix are in the S3 store.")
@click.option("--matrix-json", "matrix_json", required=True, help='A resolve-deps matrix, {"include": [...]}')
def main(matrix_json: str) -> None:
    deps = pending(json.loads(matrix_json or "{}"))
    if not deps:
        print("wait_for_deps: every dep was cached at resolve time; nothing to wait for.")
        return
    print(f"wait_for_deps: waiting on {len(deps)} artifact(s): {', '.join(name for _, _, name in deps)}")
    token = select_token()
    missing = [name for repo, sha, name in deps if not poll_for_artifact(repo, sha, name, token)]
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
