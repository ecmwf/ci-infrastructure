#!/usr/bin/env python3
"""
wait_for_artifacts.py

Block until one or more named artifacts appear in the S3 artifact store.

Used by the `dispatch-and-wait` composite action after it fires another repo's
``cross-repo-trigger.yml``. Rather than waiting for the *whole* dispatched run to
complete (build + tests + clang-tidy + valgrind + sanitizers), we only wait until
the build legs we actually care about have published their artifacts. The run may
keep doing unrelated work afterward — we no longer care once the artifact exists.

This returns as soon as the build publishes (faster) and checks existence against
the S3 store via ``head_object`` rather than the GitHub API (cheaper polling).

All real work is delegated to the tested primitives:
  * ``fetch_deps.poll_for_artifact`` — polls the store for a single artifact,
    uses the producer's workflow-run status only as a give-up signal, honors the
    ``ARTIFACT_WAIT_TIMEOUT`` / ``ARTIFACT_POLL_INTERVAL`` env knobs, and emits
    structured diagnostics when it gives up.
  * ``check_artifact.resolve_ref`` — resolves the ref to the 40-char SHA used for
    the run-status give-up probe.

Usage:
    wait_for_artifacts.py --repo owner/repo --ref <branch|tag|sha> \\
                          --artifact-names "<name1> <name2> ..."

Exits 0 only when every named artifact is present; otherwise prints an ::error::
listing the artifacts that never appeared and exits 1.
"""

from __future__ import annotations

import click

from ._errors import CIError
from ._github_api import select_token
from .check_artifact import resolve_ref
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
    sha = resolve_ref(repo, ref, token)

    print(f"wait_for_artifacts: waiting on {len(names)} artifact(s) from {repo}@{sha[:8]}: {', '.join(names)}")

    missing = [name for name in names if not poll_for_artifact(repo, sha, name, token)]

    if missing:
        raise CIError(
            f"{len(missing)} artifact(s) never appeared in the store for {repo}@{sha[:8]}: {', '.join(missing)}"
        )

    print(f"wait_for_artifacts: all {len(names)} artifact(s) present.")


if __name__ == "__main__":
    main()
