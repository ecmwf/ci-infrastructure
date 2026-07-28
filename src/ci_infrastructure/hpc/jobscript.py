# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering the SLURM job script from a repo's build recipe.

The repo owns ``.ci/hpc/build.sh`` — its ``#SBATCH`` resource directives,
``module load`` lines and its cmake/ctest body. ci-infrastructure wraps that
body with only the orchestration bits it must control:

  * ``#SBATCH --output/--error`` pointing at the path we tail for completion,
  * the dependency environment (``CMAKE_PREFIX_PATH``, ``CI_INSTALL_PREFIX``),
  * a **submit-then-poll** preamble: the job blocks until the runner drops a
    ``TRANSFER_COMPLETED_<run_id>`` marker in the shared staging dir (the runner
    submits first, then scp's the source tarball and touches the marker), then
    unpacks the checkout into node-local ``$TMPDIR`` and ``cd``s there,
  * a ``Finished: SUCCESS`` / ``Finished: FAILURE`` sentinel so the poller can
    tell the outcome (a completed SLURM job vanishes from ``squeue``, so the
    sentinel — not the scheduler — is the authoritative success signal).

The repo's leading directive block is preserved verbatim at the top so its
``#SBATCH`` header stays where SLURM requires it (before any command).
"""

from __future__ import annotations

from collections.abc import Mapping

SENTINEL_SUCCESS = "Finished: SUCCESS"
SENTINEL_FAILURE = "Finished: FAILURE"

#: Default seconds the job waits for the source-transfer marker before giving up.
DEFAULT_MARKER_WAIT_TIMEOUT = 1800

#: Fixed name of the source tarball the runner scp's into the staging dir and
#: the job unpacks. Independent of the run id so a reattach can re-drop it.
SOURCE_TARBALL_NAME = "source.tgz"


def job_name_for(artifact_name: str) -> str:
    """SLURM job name for an artifact — the key a re-run reattaches by.

    The scheduler is the shared, cross-runner job store: each job is stamped with
    this name and its submitting run id (in ``--comment``), so a second run finds
    the in-flight job by name instead of submitting a duplicate (the runner-local
    jid file this replaces was invisible to other runners). Artifact names are the
    safe ``[A-Za-z0-9_.-]`` set and well within SLURM's name length, so they are
    used verbatim under a ``ci-`` namespace prefix. Should a cluster ever cap the
    name length, hash the artifact here (``ci-<sha256(artifact)[:32]>``) and keep
    the full name only in the comment.
    """
    return f"ci-{artifact_name}"


def _split_header(repo_script: str) -> tuple[str, list[str], list[str]]:
    """Split a build.sh into (shebang, leading-directive/comment block, body).

    The header is the run of leading lines that are blank or start with ``#``
    (shebang, comments, ``#SBATCH`` / ``#TROIKA`` directives) — i.e. everything
    up to the first executable line. ``#SBATCH`` must precede any command, so
    appending our own directives at the end of this block keeps them valid.
    """
    lines = repo_script.splitlines()
    shebang = ""
    start = 0
    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        start = 1
    header: list[str] = []
    body_start = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            header.append(lines[i])
        else:
            body_start = i
            break
    return shebang, header, lines[body_start:]


def _marker_wait_block(staging_dir: str, run_id: str, marker_wait_timeout: int) -> list[str]:
    """Emit the bounded wait for the source-transfer marker + node-local unpack.

    The runner submits the job first, then scp's the source tarball into
    ``staging_dir`` and finally ``touch``es ``TRANSFER_COMPLETED_<run_id>``. The
    job blocks here until that marker appears (so it never unpacks a half-copied
    tarball), then unpacks the checkout into ``$TMPDIR`` and ``cd``s there — the
    recipe then sees its sources as ``$CI_SOURCE_DIR``, node-local for a fast
    build. A marker that never arrives is a failure (an explicit sentinel, since
    the ``ERR`` trap does not fire on a bare ``exit``).
    """
    marker = f"{staging_dir}/TRANSFER_COMPLETED_{run_id}"
    tarball = f"{staging_dir}/{SOURCE_TARBALL_NAME}"
    return [
        f'echo "ci: waiting for source-transfer marker {run_id}..."',
        f'_ci_marker="{marker}"',
        f"_ci_deadline=$(( $(date +%s) + {marker_wait_timeout} ))",
        'until [ -f "$_ci_marker" ]; do',
        '  if [ "$(date +%s)" -ge "$_ci_deadline" ]; then',
        f'    echo "ci: source-transfer marker {run_id} never arrived within {marker_wait_timeout}s" >&2',
        f'    echo "{SENTINEL_FAILURE}"',
        "    exit 1",
        "  fi",
        "  sleep 5",
        "done",
        f'export CI_SOURCE_DIR="${{TMPDIR:-/tmp}}/ci-src-{run_id}"',
        'mkdir -p "$CI_SOURCE_DIR"',
        f'tar -xzf "{tarball}" -C "$CI_SOURCE_DIR"',
        'cd "$CI_SOURCE_DIR"',
    ]


def render_job_script(
    *,
    repo_script: str,
    output_path: str,
    cmake_prefix_path: str,
    install_path: str,
    job_name: str | None = None,
    staging_dir: str | None = None,
    run_id: str | None = None,
    marker_wait_timeout: int = DEFAULT_MARKER_WAIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> str:
    """Wrap a repo's build.sh into the final submittable SLURM script.

    When ``staging_dir`` and ``run_id`` are given, the job waits for the
    runner's source-transfer marker, unpacks the checkout into node-local
    ``$TMPDIR`` and ``cd``s there before running the body — so the recipe sees
    its sources as ``$CI_SOURCE_DIR`` the same way the runner build sees
    ``$GITHUB_WORKSPACE``.
    """
    shebang, header, body = _split_header(repo_script)

    out: list[str] = [shebang or "#!/bin/bash"]
    out.extend(header)
    out.append(f"#SBATCH --output={output_path}")
    out.append(f"#SBATCH --error={output_path}")
    # Name + comment make the scheduler the shared job store: submit-wait finds an
    # in-flight job for this artifact by name and reads back the submitting run id
    # from the comment (see orchestrate.find_active_job_by_name).
    if job_name is not None:
        out.append(f"#SBATCH --job-name={job_name}")
    if run_id is not None:
        out.append(f"#SBATCH --comment={run_id}")
    out.append("")
    out.append("set -euo pipefail")
    # Prepend the resolved deps so the repo body's cmake finds them; keep any
    # pre-existing value on the tail.
    out.append(f'export CMAKE_PREFIX_PATH="{cmake_prefix_path}${{CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}}"')
    out.append(f'export CI_INSTALL_PREFIX="{install_path}"')
    for key, value in (env or {}).items():
        out.append(f'export {key}="{value}"')
    # A failure anywhere below (set -e trips ERR) prints the failure sentinel
    # before the job exits non-zero, so the trap must be armed before the
    # marker-wait/unpack and the body run.
    out.append(f'_ci_on_err() {{ echo "{SENTINEL_FAILURE}"; }}')
    out.append("trap _ci_on_err ERR")
    out.append("")
    if staging_dir is not None and run_id is not None:
        out.extend(_marker_wait_block(staging_dir, run_id, marker_wait_timeout))
        out.append("")
    out.extend(body)
    out.append("")
    out.append(f'echo "{SENTINEL_SUCCESS}"')
    return "\n".join(out) + "\n"
