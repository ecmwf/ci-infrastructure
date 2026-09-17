#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve a ref to a SHA, check whether its artifact exists, and report run state if not.

Outputs are the ``Outputs`` keys; run-status and run-conclusion follow
``_github_api.WorkflowRuns`` and are empty when the artifact was found.
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
    template_version_for_lane,
    write_outputs,
)

RunStatus: TypeAlias = Literal["running", "completed", "none"]
RunConclusion: TypeAlias = Literal["success", "failure"]

# Functional syntax: the keys are hyphenated $GITHUB_OUTPUT names.
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
    help="Binary-compatibility class (e.g. ubuntu-24.04), used verbatim as the artifact-name platform slot.",
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
    help="Space-separated direct dependency artifact names; hashed into the deps-hash8 segment. Empty for leaves.",
)
@click.option(
    "--options",
    "options",
    default="",
    help="Build-option config name; appends an 'opts.<name>' segment when non-empty.",
)
@click.option(
    "--lane",
    "lane",
    type=click.Choice(["runner", "hpc"]),
    default="runner",
    help="Execution lane of the build. hpc artifacts carry the HPC template version segment.",
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
    lane: str,
) -> None:
    # An empty slot would mint a malformed name (e.g. "ecbuild--Release") nothing can satisfy.
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
        template_version=template_version_for_lane("hpc" if lane == "hpc" else "runner"),
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
            "(wrong compiler / platform / build-type?)"
        )
    else:
        print(f"Artifact not found: {artifact_name} — no upstream workflow runs found for this SHA")


if __name__ == "__main__":
    main()
