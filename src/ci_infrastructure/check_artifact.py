#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# (C) Copyright 2026 - ECMWF and individual contributors.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation nor
# does it submit to any jurisdiction.

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
compiler versions are stored and retrieved independently
(e.g. gfortran-14, clang-18, gcc-13).

The <platform> segment is the required --platform value, used verbatim (the
explicit binary-compatibility class) — same as resolve_deps. The runner/image
is pure scheduling and is not part of artifact identity. The slug's first
hyphen-separated segment must not be exactly 8 hex chars (would collide with
the deps-hash8 segment).
"""

import os
import re
from typing import Final, Literal, TypeAlias, TypedDict, cast

import click

from . import s3_store
from ._errors import CIError
from ._github_api import (
    IN_PROGRESS_STATUSES,
    canonical_option_segment,
    compute_deps_hash8,
    compute_platform_slug,
    gh_api_rest,
    select_token,
)

RunStatus: TypeAlias = Literal["running", "completed", "none"]
RunConclusion: TypeAlias = Literal["success", "failure"]

_FAILURE_CONCLUSIONS: Final = frozenset({"failure", "cancelled", "timed_out", "action_required", "startup_failure"})


# Functional TypedDict syntax supports hyphenated keys; two-class inheritance
# gives us a required base and an optional extension without needing NotRequired
# (which requires Python ≥ 3.11).
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


def resolve_ref(repo: str, ref: str, token: str | None) -> str:
    """Resolve a branch / lightweight or annotated tag / short or full SHA to a 40-char commit SHA.

    Uses the commits/{ref} REST endpoint, which auto-disambiguates and dereferences
    annotated tags to the underlying commit (the tags/{ref} endpoint we used previously
    returned the tag-object SHA for annotated tags, which doesn't match how artifacts
    are keyed). Mirrored in resolve_deps.resolve_ref_to_sha.
    """
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    data = gh_api_rest(f"repos/{repo}/commits/{ref}", token)
    if isinstance(data, dict) and isinstance(data.get("sha"), str):
        return cast(str, data["sha"])
    raise CIError(f"Could not resolve ref '{ref}' in {repo}")


def find_workflow_run_status(repo: str, sha: str, token: str | None) -> tuple[RunStatus, RunConclusion | None]:
    """
    Check whether any workflow run exists for the given commit SHA.

    Returns (run_status, run_conclusion):
      ("running",   None)        — at least one run is queued or in_progress
      ("completed", "failure")   — all runs done; at least one failed/cancelled
      ("completed", "success")   — all runs done and all succeeded
      ("none",      None)        — no runs found for this SHA
    """
    data = gh_api_rest(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100", token)
    if not isinstance(data, dict):
        return ("none", None)
    runs = data.get("workflow_runs", [])
    if not isinstance(runs, list) or len(runs) == 0:
        return ("none", None)

    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("status") in IN_PROGRESS_STATUSES:
            return ("running", None)

    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("conclusion") in _FAILURE_CONCLUSIONS:
            return ("completed", "failure")

    return ("completed", "success")


def write_outputs(outputs: Outputs) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            for key, value in outputs.items():
                # GitHub Actions outputs are stringly-typed: collapse None to empty
                # string only here, at the wire boundary.
                f.write(f"{key}={'' if value is None else value}\n")
    else:
        for key, value in outputs.items():
            print(f"{key}={value}")


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
    sha = resolve_ref(repo, ref, token)
    deps_hash8 = compute_deps_hash8(deps_artifact_names.split())

    if python_version:
        if deps_hash8:
            artifact_name = (
                f"{artifact_prefix}-{sha}-{deps_hash8}-{platform_slug}-{compiler}-py{python_version}-{build_type}"
            )
        else:
            artifact_name = f"{artifact_prefix}-{sha}-{platform_slug}-{compiler}-py{python_version}-{build_type}"
    else:
        if deps_hash8:
            artifact_name = f"{artifact_prefix}-{sha}-{deps_hash8}-{platform_slug}-{compiler}-{build_type}"
        else:
            artifact_name = f"{artifact_prefix}-{sha}-{platform_slug}-{compiler}-{build_type}"
    # Orthogonal options axis: append the shared segment (empty -> no change).
    opt_seg = canonical_option_segment(options.strip())
    if opt_seg:
        artifact_name = f"{artifact_name}-{opt_seg}"
    tar_name = f"{artifact_name}.tar.gz"
    found = s3_store.object_exists(artifact_name)
    run_status: RunStatus | None
    run_conclusion: RunConclusion | None
    if found:
        run_status, run_conclusion = None, None
    else:
        run_status, run_conclusion = find_workflow_run_status(repo, sha, token)

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
