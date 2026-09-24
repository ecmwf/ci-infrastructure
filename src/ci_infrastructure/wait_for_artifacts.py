#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Block until named artifacts appear in the S3 store (used by `dispatch-and-wait`)."""

from __future__ import annotations

import click

from ._errors import CIError
from ._github_api import resolve_ref_to_sha, select_token
from .fetch_deps import poll_for_artifact


@click.command(help="Wait until named artifacts appear in the S3 store.")
@click.option("--repo", required=True, help="owner/repo that builds and publishes the artifacts")
@click.option("--ref", required=True, help="branch, tag, or SHA the dispatched run builds")
@click.option(
    "--artifact-names",
    "artifact_names",
    required=True,
    help="Space-separated list of fully-computed artifact names to wait for.",
)
def main(repo: str, ref: str, artifact_names: str) -> None:
    names = artifact_names.split()
    if not names:
        raise CIError("wait_for_artifacts: --artifact-names was empty.")

    token = select_token()
    sha = resolve_ref_to_sha(repo, ref, token)

    print(f"wait_for_artifacts: waiting on {len(names)} artifact(s) from {repo}@{sha[:8]}: {', '.join(names)}")

    missing = [name for name in names if not poll_for_artifact(repo, sha, name, token)]

    if missing:
        raise CIError(
            f"{len(missing)} artifact(s) never appeared in the store for {repo}@{sha[:8]}: {', '.join(missing)}"
        )

    print(f"wait_for_artifacts: all {len(names)} artifact(s) present.")


if __name__ == "__main__":
    main()
