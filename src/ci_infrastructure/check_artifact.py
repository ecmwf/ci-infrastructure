#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
check_artifact.py

Resolves a git ref to a full SHA and checks whether a named artifact already
exists in a GitHub repository's artifact store. Also checks whether a workflow
run for that SHA is currently in progress, so callers can wait intelligently.

Writes the following key=value pairs to $GITHUB_OUTPUT (or stdout if unset):
  sha=<40-char SHA>
  artifact-name=<prefix>-<sha>[-<deps-hash8>]-<platform>-<compiler>-<build-type>[-opts.<option>]
  tar-name=<artifact-name>.tar.gz
  found=true|false
  original-ref=<the --ref value passed in, for threading into merge>
  run-status=running|completed|none
    running   — at least one workflow run for this SHA is queued or in_progress
    completed — all runs for this SHA have finished
    none      — no workflow runs found for this SHA
  run-conclusion=success|failure|cancelled|skipped|  (empty when run-status != completed)
    When multiple runs exist and any failed, conclusion is 'failure'.
    When all succeeded, conclusion is 'success'.

When --deps-artifact-names is provided, a deps-hash8 segment is inserted into
the artifact name after the SHA:
  <prefix>-<sha>-<deps-hash8>-<platform>-<compiler>[-py<ver>]-<build-type>

deps-hash8 is the first 8 characters of the SHA-256 of the sorted,
space-separated dep artifact names. This forms a Merkle tree so that
re-compiling an upstream package invalidates all downstream artifact keys.

Leaf packages with no compiled binary dependencies (e.g. fortmath) should
not pass --deps-artifact-names; their artifact names stay in the original
format without the deps-hash segment.

The --compiler value should include the version so that builds for different
compiler versions are stored and retrieved independently (e.g. gfortran-14,
clang-18, gcc-13).

The <platform> segment is the required --platform value, used verbatim (the
explicit binary-compatibility class) — same as resolve_deps. The runner/image
is pure scheduling and is not part of artifact identity. The slug's first
hyphen-separated segment must not be exactly 8 hex chars (would collide with
the deps-hash8 segment).
"""

from typing import Literal, TypeAlias, TypedDict

import click

from . import s3_store
from ._errors import CIError
from ._github_api import (
    compute_deps_hash8,
    compute_platform_slug,
    make_artifact_name,
    probe_workflow_runs,
    resolve_ref_to_sha,
    select_token,
    write_outputs,
)

RunStatus: TypeAlias = Literal["running", "completed", "none"]
RunConclusion: TypeAlias = Literal["success", "failure"]

# Functional TypedDict syntax, because the keys are hyphenated — they are the
# literal $GITHUB_OUTPUT names, so they cannot be identifiers.
#
# `run-status` / `run-conclusion` are stored as Optional internally and only
# get materialised to the empty string at the $GITHUB_OUTPUT boundary in
# write_outputs — so absence is a real None in the type system, not a "" sentinel.
Outputs = TypedDict(
    "Outputs",
    {
        "sha": str,
        "artifact-name": str,
        "tar-name": str,
        "found": Literal["true", "false"],
        "original-ref": str,
        "run-status": RunStatus | None,
        "run-conclusion": RunConclusion | None,
    },
)


@click.command(help="Resolve a ref and check the GitHub artifact store.")
@click.option("--repo", required=True, help="owner/repo to check")
@click.option("--ref", required=True, help="branch, tag, or SHA")
@click.option("--artifact-prefix", "artifact_prefix", required=True, help="prefix for artifact name")
@click.option(
    "--platform",
    required=True,
    help=(
        "Explicit binary-compatibility class (e.g. ubuntu-24.04), used verbatim as the "
        "artifact-name platform slot. ABI-compatible images sharing a platform share one "
        "artifact. The runner/image is pure scheduling and is not part of artifact identity."
    ),
)
@click.option(
    "--compiler",
    required=True,
    help="compiler identifier including version (e.g. gfortran-14, clang-18)",
)
@click.option(
    "--build-type",
    "build_type",
    required=True,
    help="CMake build type (e.g. Release, Debug)",
)
@click.option(
    "--python-version",
    "python_version",
    default="",
    help="Python version (e.g. 3.12). When set, included in artifact name as py<version>.",
)
@click.option(
    "--deps-artifact-names",
    "deps_artifact_names",
    default="",
    help=(
        "Space-separated list of direct compiled dependency artifact names. "
        "When provided, a deps-hash8 segment is inserted into the artifact name "
        "after the SHA to form a Merkle tree. Leave empty for leaf packages "
        "(e.g. fortmath) that have no compiled binary dependencies."
    ),
)
@click.option(
    "--options",
    "options",
    default="",
    help=(
        "Scalar build-option config name (feature config) for this build. When "
        "non-empty, an 'opts.<name>' segment is appended to the artifact name. "
        "Leave empty for a plain build (name unchanged)."
    ),
)
def main(
    repo: str,
    ref: str,
    artifact_prefix: str,
    platform: str,
    compiler: str,
    build_type: str,
    python_version: str,
    deps_artifact_names: str,
    options: str,
) -> None:
    # An empty value in any required slot would silently produce a malformed
    # artifact name (e.g. "ecbuild--Release") that downstream poll loops can
    # never satisfy. Catch that at the boundary with a specific diagnostic
    # rather than guessing later from the malformed name.
    required = {
        "--repo": repo,
        "--ref": ref,
        "--artifact-prefix": artifact_prefix,
        "--platform": platform,
        "--compiler": compiler,
        "--build-type": build_type,
    }
    empty = [flag for flag, val in required.items() if not val or not val.strip()]
    if empty:
        raise CIError(
            f"check_artifact: required flag(s) empty: {', '.join(empty)}. "
            "An upstream step (often a jq decode of matrix-leg) likely returned empty."
        )

    try:
        platform_slug = compute_platform_slug(platform)
    except ValueError as e:
        raise CIError(str(e)) from e

    token = select_token()
    sha = resolve_ref_to_sha(repo, ref, token)
    artifact_name = make_artifact_name(
        prefix=artifact_prefix,
        sha=sha,
        deps_hash8=compute_deps_hash8(deps_artifact_names.split()),
        platform_slug=platform_slug,
        compiler=compiler,
        build_type=build_type,
        python_version=python_version or None,
        option=options.strip(),
    )
    tar_name = f"{artifact_name}.tar.gz"
    found = s3_store.object_exists(artifact_name)
    run_status: RunStatus | None
    run_conclusion: RunConclusion | None
    if found:
        run_status, run_conclusion = None, None
    else:
        runs = probe_workflow_runs(repo, sha, token)
        run_status, run_conclusion = runs.state, runs.conclusion

    outputs: Outputs = {
        "sha": sha,
        "artifact-name": artifact_name,
        "tar-name": tar_name,
        "found": "true" if found else "false",
        "original-ref": ref,
        "run-status": run_status,
        "run-conclusion": run_conclusion,
    }

    write_outputs(outputs)

    if found:
        print(f"Artifact found: {artifact_name}")
    elif run_status == "running":
        print(f"Artifact not yet ready: {artifact_name} — upstream build is in progress")
    elif run_status == "completed" and run_conclusion == "failure":
        print(f"Artifact missing: {artifact_name} — upstream build completed with failure")
    elif run_status == "completed" and run_conclusion == "success":
        print(
            f"Artifact missing: {artifact_name} — upstream build succeeded but artifact not found "
            "(wrong compiler / runs-on / container?)"
        )
    else:
        print(f"Artifact not found: {artifact_name} — no upstream workflow runs found for this SHA")


if __name__ == "__main__":
    main()
