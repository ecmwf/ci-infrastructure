#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
orchestrate.py — submit / wait / cancel a SLURM build job via troika (as a library).

This is the HPC counterpart of the runner build step. It is invoked by the
``build-on-hpc`` composite action on a login-node self-hosted runner:

    python -m ci_infrastructure.hpc submit-wait --site hpc-batch \\
        --job-script ./.ci/hpc/build.sh --artifact-name <name> \\
        --remote-work-dir '$SCRATCH/github-ci' --local-install-path <local>/install/<name> \\
        --source-dir <workspace> --run-id <run>-<attempt> \\
        --tar-dir <local>/hpc-tars --cmake-prefix-path <prefix>

Design points that mirror the non-HPC path and satisfy the requirements:

  * Orchestration is pure Python calling troika's ``Site`` API directly — no
    shell-out to the troika CLI. The SLURM script itself is bash (it runs on a
    compute node); everything around it is Python.

  * The cluster work dir is expanded on the cluster (see
    ``site.resolve_remote_path``) and the per-artifact layout derived from it
    (see ``RemotePaths``), so the configured value stays portable
    (``$SCRATCH/github-ci``) while every path the runner uses is literal.

  * Submit-then-poll transfer. The job is submitted first (claiming its queue
    slot), then the runner scp's the source tarball into the shared staging dir
    and ``touch``es ``TRANSFER_COMPLETED`` in it. The job blocks on that marker,
    unpacks the checkout into node-local ``$TMPDIR`` and builds. A reattach
    re-checks the marker and re-ships only if it is missing. The marker is named
    for the staging dir (already per-artifact), not for the submitting run, so a
    reattaching runner needs nothing from the job it adopts beyond its jid.

  * Restartable / idempotent. On every (re-)run ``submit-wait``:
      (a) if the artifact already exists in the S3 store -> skip (cache hit);
      (b) if a SLURM job for this artifact is still active -> reattach and wait
          (no duplicate job);
      (c) otherwise submit a fresh job.
    Reattach uses the *scheduler itself* as the shared job store: each job is
    stamped with a stable per-artifact name, and ``submit-wait`` finds an
    in-flight job by name before submitting. Because the scheduler is global,
    this dedups across independent runners — a runner-local record could not.
    The name is all we read back; the ``--comment`` we also write is provenance
    for humans, never parsed (see ``find_active_job_by_name``).

  * Rate-friendly polling. A completed SLURM job disappears from ``squeue``, so
    the job's ``Finished: SUCCESS`` / ``Finished: FAILURE`` output sentinel — not
    the scheduler — is the authoritative outcome. We block on a single persistent
    ``tail -F | grep`` of the output (one held connection, near-zero scheduler
    load) and only touch ``squeue`` (via ``_get_state``) as a low-frequency
    liveness guard so a job that dies without a sentinel can't hang us forever.
    The waiter fails closed: only a sentinel it actually read is a verdict.

Batch (slurm) sites only — see ``site.ensure_batch_site``.
"""

from __future__ import annotations

import random
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Any, Final, Literal, NamedTuple

import click

from .. import s3_store
from .._errors import CIError
from .._github_api import write_outputs
from . import jobscript, transfer
from .site import SlurmSiteLike, ensure_batch_site, load_site, resolve_remote_path

# SLURM states that mean "the job is still going"; anything else (or the job
# having vanished from squeue) means it is no longer running.
ACTIVE_STATES: Final = frozenset(
    {"PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "RESIZING", "SUSPENDED", "REQUEUED"}
)

_DEFAULT_GUARD_INTERVAL: Final = 120  # seconds between squeue liveness checks
_DEFAULT_WAIT_TIMEOUT: Final = 6 * 60 * 60  # 6h hard ceiling on a single job wait
_GRACE_SECONDS: Final = 10  # last look for a late-flushed sentinel after the job leaves the queue
_WAITER_GRACE_SECONDS: Final = 15  # slack over the remote timeout before we give up on the tail itself

Verdict = Literal["SUCCESS", "FAILURE", "VANISHED", "TIMEOUT"]


# --------------------------------------------------------------------------- #
# The cluster-side layout
# --------------------------------------------------------------------------- #
def write_job_script(path: Path, rendered: str) -> None:
    """Write a rendered job script, executable.

    ``sbatch`` reads the script rather than executing it, so the bit is not
    required to submit; it is here so that a rendered script left behind after a
    failure can be run directly while debugging.
    """
    path.write_text(rendered)
    path.chmod(0o755)


class RemotePaths(NamedTuple):
    """Where one artifact's build lives under the (resolved) cluster work dir.

    All three are on the shared filesystem: the compute node reads the staged
    tarball and marker, writes the install tree, and both sides touch the output.
    Only the *unpack* target is node-local (``$TMPDIR``, chosen by the job script).
    """

    output: str
    install: str
    staging: str

    @classmethod
    def derive(cls, work_dir: str, artifact_name: str) -> RemotePaths:
        base = PurePosixPath(work_dir)
        return cls(
            output=str(base / "hpc-jobs" / f"{artifact_name}.out"),
            install=str(base / "install" / artifact_name),
            staging=str(base / "staging" / artifact_name),
        )


def plan_remote_prefixes(cmake_prefix_path: str, staging_dir: str) -> tuple[str, list[str], str]:
    """Map runner-local dependency prefixes to the cluster paths they are shipped to.

    The dep install trees in ``cmake_prefix_path`` live on the runner, which the
    compute node cannot see, so ``transfer.ship_source`` unpacks each into
    ``<staging_dir>/deps/<i>`` on the shared filesystem. This returns the
    ``CMAKE_PREFIX_PATH`` to bake into the job (those cluster paths, in order),
    the ordered local prefixes to ship, and the remote deps dir. A build with no
    deps yields ``("", [], …)`` — its job script is byte-for-byte unchanged.
    """
    local_prefixes = [p for p in re.split(r"[;:]", cmake_prefix_path) if p]
    remote_deps_dir = f"{staging_dir.rstrip('/')}/deps"
    remote_prefixes = [f"{remote_deps_dir}/{index}" for index in range(len(local_prefixes))]
    return ":".join(remote_prefixes), local_prefixes, remote_deps_dir


# --------------------------------------------------------------------------- #
# Reattach lookup (the scheduler is the shared, cross-runner job store)
# --------------------------------------------------------------------------- #
def find_active_job_by_name(conn: Any, *, job_name: str, user: str | None) -> int | None:
    """Return the jid of an active SLURM job named ``job_name``, or None.

    Reattach uses the scheduler as the shared job store: a job is stamped with a
    stable per-artifact name (``jobscript.job_name_for``). Querying by name lets a
    second run on a *different* runner reattach to an in-flight job instead of
    submitting a duplicate — the runner-local jid file this replaces was invisible
    across runners.

    The NAME is the whole of what we read back. We deliberately do not ask for the
    job's ``Comment`` (``%k``), even though we write our run id into it: that field
    belongs to the scheduler and sites rewrite it. ECMWF's sbatch wrapper appends
    its own accounting fields, so the value returned here was
    ``<run>-<attempt>;Gres=gres/ssdtmp:20G;`` — and because the caller then used it
    to name a marker and a local tarball, every reattaching HPC job died on
    ``tar -czf .../<that>.src.tgz``. Nothing needs it now: the transfer marker is
    named for the staging dir (``jobscript.TRANSFER_MARKER_NAME``), so a
    reattaching runner can check and re-drop it without knowing who submitted.

    ``squeue -h -n <name> -t <active states> -o '%i'`` prints one jid per matching
    job. Empty output -> no active job -> the caller submits fresh. More than one
    line means two runs raced the submit (the residual window reattach-only
    accepts); we take the lowest jid so every later run converges on the same one,
    and warn. A ``squeue`` that itself errors yields None (fail open -> submit),
    matching the prior ``_get_state(strict=False)`` behaviour.
    """
    states = ",".join(sorted(ACTIVE_STATES))
    argv = ["squeue", "-h", "-n", job_name, "-t", states, "-o", "%i"]
    if user:
        argv += ["-u", user]
    proc = conn.execute(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stdout, _stderr = proc.communicate()
    if proc.returncode != 0:
        return None
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)
    jids: list[int] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Tolerate a trailing field should a site's squeue ever add one.
        try:
            jids.append(int(line.split("|", 1)[0].strip()))
        except ValueError:
            continue
    if not jids:
        return None
    jids.sort()
    if len(jids) > 1:
        print(f"submit-wait: WARNING: {len(jids)} jobs named {job_name!r} (jids {jids}); reattaching to {jids[0]}.")
    return jids[0]


# --------------------------------------------------------------------------- #
# Submit / reattach / wait / cancel
# --------------------------------------------------------------------------- #
def submit_or_reattach(
    *,
    site: SlurmSiteLike,
    script_path: Path,
    user: str | None,
    output: str,
    job_name: str,
    after_submit: Callable[[], None] | None = None,
    dryrun: bool = False,
) -> tuple[int, Literal["submitted", "reattached", "dryrun"]]:
    """Reattach to a still-active job for this artifact, else submit a fresh one.

    Reattach is by scheduler name (``find_active_job_by_name``): if an active job
    named ``job_name`` exists, we adopt its jid. That is all we take from the
    scheduler — see that function for why the job's ``--comment`` is not read.
    This dedups across independent runners.

    Submit-then-poll: on a fresh submit the job is submitted first (claiming its
    queue slot), then ``after_submit`` ships the source and drops the transfer
    marker the job is waiting on. A reattach never re-ships, so an in-flight job's
    sources are left untouched (the caller re-checks the marker for that case).

    Returns ``(jid, action)``. ``jid`` is -1 for a dry run.
    """
    if not dryrun:
        found = find_active_job_by_name(site._connection, job_name=job_name, user=user)
        if found is not None:
            return found, "reattached"

    # troika's submit() scp's the script into the output directory on the remote,
    # but the mkdir -p that would create it lives in the `create_output_dir`
    # pre_submit hook, which only runs through troika's controller. We drive the
    # site as a library and skip the controller, so we run that step ourselves.
    site.create_output_dir(output, dryrun=dryrun)
    # Create the file the waiter tails, so an output path we cannot write is a
    # failure here rather than a job that runs to completion with nowhere to
    # report it. SLURM truncates it when the job starts; `tail -F` follows that.
    if not dryrun:
        transfer.touch_remote_file(site._connection, path=output)

    jid = site.submit(str(script_path), user, output, dryrun=dryrun)
    if dryrun:
        return -1, "dryrun"
    jid = int(jid)
    if after_submit is not None:
        after_submit()
    return jid, "submitted"


def wait_for_job(
    *,
    sentinel_waiter: Callable[[float], Verdict | None],
    state_getter: Callable[[], str | None],
    timeout: float = _DEFAULT_WAIT_TIMEOUT,
    guard_interval: float = _DEFAULT_GUARD_INTERVAL,
    jitter: float = 0.1,
) -> Verdict:
    """Block until the job's outcome is known.

    ``sentinel_waiter(seconds)`` blocks up to ``seconds`` for the output
    sentinel, returning ``"SUCCESS"``/``"FAILURE"`` if it appears or ``None`` on
    timeout. ``state_getter()`` returns the scheduler state (or ``None`` if the
    job is gone). The scheduler is only consulted once per ``guard_interval`` (a
    jittered cadence to avoid many parallel jobs hitting ``squeue`` in lockstep).
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "TIMEOUT"
        # Jitter the liveness cadence so concurrent jobs don't poll in lockstep.
        window = min(guard_interval * (1.0 + random.uniform(-jitter, jitter)), remaining)
        verdict = sentinel_waiter(window)
        if verdict is not None:
            return verdict
        # Sentinel didn't appear in this window — is the job still alive?
        if state_getter() is None:
            # Gone from the queue with no sentinel yet: give the output one last
            # grace look (the sentinel may still be flushing), then declare it dead.
            final = sentinel_waiter(_GRACE_SECONDS)
            return final if final is not None else "VANISHED"


def cancel_job(
    *,
    site: SlurmSiteLike,
    script_path: Path,
    output: str,
    jid: int,
    dryrun: bool = False,
) -> tuple[int, str | None]:
    """Cancel a running job (troika has no restart verb; restart == resubmit)."""
    return site.kill(str(script_path), None, output, jid=jid, dryrun=dryrun)


# --------------------------------------------------------------------------- #
# Cleanup (nightly GC of the cluster work dir)
# --------------------------------------------------------------------------- #
#: A run id becomes a path segment (the local source tarball, the ``.trash.<id>``
#: staging rename, the node-local ``ci-src-<id>``), so it may not contain a
#: separator or shell metacharacter. Mirrors site._SAFE_SPEC in intent.
_SAFE_RUN_ID: Final = re.compile(r"^[A-Za-z0-9._-]+$")


#: Subdirectories of the remote work dir that run_gc sweeps by age. Each holds
#: one entry per artifact (or a .out file for hpc-jobs), so a maxdepth-1 sweep
#: reclaims whole builds without touching the roots. "transfer-e2e" is not
#: written by the build path: smoke-test-hpc.yml puts its per-run tree there so
#: a failed round-trip, which deliberately leaves the tree behind for debugging,
#: is still reclaimed eventually. Anything written directly under the work dir
#: instead of one of these is never swept.
GC_SUBDIRS: Final = ("staging", "install", "hpc-jobs", "transfer-e2e")


def run_gc(conn: Any, *, remote_work_dir: str, older_than_days: int, dryrun: bool = False) -> None:
    """Remove per-artifact trees under the remote work dir older than N days.

    Runs one ``find ... -mtime +N`` per subdir over troika's connection. A
    missing subdir is skipped (``test -d``), so a partially-used work dir is
    fine. ``dryrun`` lists candidates instead of deleting them.
    """
    action = "-print" if dryrun else "-exec rm -rf {} +"
    for sub in GC_SUBDIRS:
        base = f"{remote_work_dir.rstrip('/')}/{sub}"
        quoted = shlex.quote(base)
        find = f"find {quoted} -mindepth 1 -maxdepth 1 -mtime +{older_than_days} {action}"
        proc = conn.execute(
            ["bash", "-c", f"test -d {quoted} && {find} || true"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate()
        listing = stdout.decode(errors="replace").strip() if isinstance(stdout, bytes) else str(stdout).strip()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr).strip()
            raise CIError(f"GC of {base} failed (exit {proc.returncode}): {detail}")
        count = len(listing.splitlines()) if listing else 0
        verb = "would remove" if dryrun else "removed"
        print(f"gc: {base}: {verb} {count} entries")


def _install_cancel_handler(site: SlurmSiteLike, script_path: Path, output: str, jid: int) -> None:
    """Scancel the SLURM job when GitHub cancels the step (SIGINT/SIGTERM).

    A cancelled GitHub job would otherwise leave the batch job running on the
    cluster. We cancel it, then exit non-zero so the step reflects the cancel.
    """

    def handler(signum: int, frame: FrameType | None) -> None:
        print(f"submit-wait: received signal {signum}; cancelling HPC job {jid}...")
        try:
            cancel_job(site=site, script_path=script_path, output=output, jid=jid)
        except Exception as exc:  # best-effort: never mask the cancellation itself
            print(f"submit-wait: cancel of job {jid} failed: {exc}")
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _remote_sentinel_waiter(conn: Any, output: str) -> Callable[[float], Verdict | None]:
    """Build a sentinel waiter that tails the job output over troika's connection.

    Holds a single ``tail -F | grep -m1`` on the remote output. ``grep`` exits at
    the first sentinel and *prints* it, so the verdict is read from the matched
    line rather than smuggled through an exit code. That matters: a queued job
    has no output file yet (SLURM only creates it at start), and an exit-code
    scheme cannot distinguish "the job said SUCCESS" from "there was nothing to
    read" — which would report a build finished before it ever ran. Here anything
    other than a printed sentinel (missing file, dropped connection, timeout)
    yields ``None``, so the waiter fails closed and the caller re-checks
    liveness and re-establishes the tail.

    ``tail -F`` (rather than ``-f``) keeps retrying a path that does not exist
    yet and survives the truncation SLURM does when the job starts writing.

    The window is bounded *remotely* by ``timeout`` wrapping ``tail``: when it
    expires ``grep`` sees EOF and the whole pipeline exits on its own. Killing
    the local process instead would only reap the shell — the ``tail``/``grep``
    it spawned would survive holding the pipe open, and reading their output
    would then block forever.
    """
    pattern = f"^({jobscript.SENTINEL_SUCCESS}|{jobscript.SENTINEL_FAILURE})$"
    quoted_output = shlex.quote(output)
    quoted_pattern = shlex.quote(pattern)

    def wait(seconds: float) -> Verdict | None:
        window = max(1, int(seconds))
        pipeline = f"timeout {window} tail -F -n +1 {quoted_output} 2>/dev/null | grep -m1 -E {quoted_pattern}"
        proc = conn.execute(["bash", "-c", pipeline], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            # The remote timeout ends the pipeline; this is only a backstop for a
            # wedged connection.
            stdout, _stderr = proc.communicate(timeout=window + _WAITER_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None
        if proc.returncode != 0:
            return None
        matched = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)
        if jobscript.SENTINEL_SUCCESS in matched:
            return "SUCCESS"
        if jobscript.SENTINEL_FAILURE in matched:
            return "FAILURE"
        return None

    return wait


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _echo_remote_output(conn: Any, output: str) -> None:
    """Print the job's captured cluster output to the runner console.

    The sentinel waiter only greps the output for the ``Finished:`` line, so on
    its own the step would surface none of the job's own output — the compiler
    messages, ctest results, the recipe's echoes. Once the job is done we read
    the whole captured output back over the connection and print it, so it lands
    in the CI step log on success and failure alike. Best-effort: a failure to
    read the log must not mask the job's actual verdict.
    """
    try:
        proc = conn.execute(["cat", output], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _ = proc.communicate()
    except Exception as exc:  # noqa: BLE001 - never let log retrieval mask the verdict
        print(f"submit-wait: could not read job output {output}: {exc}")
        return
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)
    print(f"----- HPC job output ({output}) -----")
    print(text, end="" if text.endswith("\n") else "\n")
    print("----- end HPC job output -----")


def _stream_job_output(conn: Any, output: str) -> Any:
    """Live-stream the job's output to the runner console; return the streamer process.

    Display only — the verdict still comes from ``wait_for_job``'s sentinel +
    squeue liveness logic; this just lets the compiler/ctest output appear as it
    is produced instead of only after the job ends. ``tail -F`` tolerates the
    queued job's not-yet-created output and the truncation SLURM does when the
    job starts; the ``sed`` prints every line and quits the remote pipeline the
    moment a sentinel line appears, so a finished job's streamer ends on its own.
    ``timeout`` bounds it so a job that vanishes without a sentinel cannot leave a
    tail lingering on the login node. troika sends ``stdout=None`` to
    ``/dev/null``, so we hand it the runner's own stdout explicitly — otherwise
    the stream would be silently discarded.
    """
    ceiling = int(_DEFAULT_WAIT_TIMEOUT + _WAITER_GRACE_SECONDS)
    quoted_output = shlex.quote(output)
    sed_quit = f"/^{jobscript.SENTINEL_SUCCESS}$/q; /^{jobscript.SENTINEL_FAILURE}$/q"
    pipeline = f"timeout {ceiling} tail -F -n +1 {quoted_output} 2>/dev/null | sed '{sed_quit}'"
    sys.stdout.flush()
    return conn.execute(["bash", "-c", pipeline], stdout=sys.stdout)


def _stop_stream(proc: Any) -> None:
    """Best-effort stop the live-output streamer (it may already have quit at the sentinel)."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - escalate to kill; never raise from cleanup
                proc.kill()
    except Exception:  # noqa: BLE001 - a streamer that cannot be stopped must not fail the build
        pass
    sys.stdout.flush()


def _site_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """The three troika-site options every subcommand takes.

    Applied as a decorator so a new subcommand cannot drift from the flag names
    the composite actions in actions/ pass verbatim.
    """
    for option in reversed(
        [
            click.option("--site", "site_name", required=True, help="Troika site name (see troika-config.yml)"),
            click.option(
                "--troika-config", "troika_config", default=None, help="Path to troika config (default: packaged)"
            ),
            click.option("--troika-user", "troika_user", default=None, help="Remote/scheduler user for troika"),
        ]
    ):
        command = option(command)
    return command


def _resolve_reported(command: str, conn: Any, remote_dir: str) -> str:
    """Expand a cluster path spec and say so when it changed.

    Every path-taking subcommand does this: the spec may name cluster variables
    ('$SCRATCH/...') that only the cluster can expand, and echoing the result is
    what makes a wrong work dir diagnosable from the runner log alone.
    """
    resolved = resolve_remote_path(conn, remote_dir)
    if resolved != remote_dir:
        print(f"{command}: remote dir {remote_dir!r} -> {resolved}")
    return resolved


@click.group(help="Submit / wait / cancel a SLURM build job via troika.")
def main() -> None:
    pass


@main.command("submit-wait", help="Submit (or reattach to) a build job and wait for it to finish.")
@_site_options
@click.option("--job-script", "job_script", required=True, help="Path to the repo's .ci/hpc/build.sh")
@click.option("--artifact-name", "artifact_name", required=True, help="Artifact name (identity, job-name + cache key)")
@click.option(
    "--remote-work-dir",
    "remote_work_dir",
    required=True,
    help="Cluster work dir, on a compute-node-visible FS. May name cluster variables "
    "(e.g. '$SCRATCH/github-ci'); they are expanded on the cluster, not here.",
)
@click.option(
    "--local-install-path",
    "local_install_path",
    required=True,
    help="Runner-local dir the built tree is fetched into (published + cache path)",
)
@click.option("--source-dir", "source_dir", default="", help="Runner-local checkout tarred and shipped to the cluster")
@click.option(
    "--run-id",
    "run_id",
    default="",
    help=(
        "Unique id of this submission (<gh-run-id>-<attempt>); names the local "
        "source tarball and the node-local source dir"
    ),
)
@click.option(
    "--marker-wait-timeout",
    "marker_wait_timeout",
    type=int,
    default=jobscript.DEFAULT_MARKER_WAIT_TIMEOUT,
    help="Seconds the job waits for the source-transfer marker before failing",
)
@click.option("--tar-dir", "tar_dir", required=True, help="Runner-local scratch dir for shipped/fetched tarballs")
@click.option("--cmake-prefix-path", "cmake_prefix_path", default="", help="Resolved dependency prefixes")
@click.option("--dryrun", is_flag=True, default=False, help="Render + go through troika in dry-run mode; do not submit")
@click.option(
    "--no-publish",
    "no_publish",
    is_flag=True,
    default=False,
    help="Test-only mode: run the job for its pass/fail (sentinel) but produce no artifact — "
    "skip the artifact cache short-circuit (a test must run every time) and skip fetching the "
    "install tree on success. For pure-test HPC legs that have nothing to build or publish.",
)
def submit_wait(
    site_name: str,
    troika_config: str | None,
    troika_user: str | None,
    job_script: str,
    artifact_name: str,
    remote_work_dir: str,
    local_install_path: str,
    source_dir: str,
    run_id: str,
    marker_wait_timeout: int,
    tar_dir: str,
    cmake_prefix_path: str,
    dryrun: bool,
    no_publish: bool,
) -> None:
    # (a) Cache hit: the artifact already exists — nothing to build. This is what
    # makes a GitHub re-run cheap and makes downstream reuse work identically to
    # the non-HPC path. The publish/print steps read the runner-local path.
    # The run id names a local tarball, the moved-aside staging tree and the
    # node-local source dir, so it must stay a single safe path segment. It comes
    # from our own --run-id and should always be `<gh-run-id>-<attempt>`; this is
    # the guard that keeps it that way. Its absence is what turned a scheduler
    # field leaking a '/' into `tar: .../<run>;Gres=gres/ssdtmp:20G;.src.tgz: No
    # such file or directory` rather than a named error.
    if run_id and not _SAFE_RUN_ID.fullmatch(run_id):
        raise CIError(
            f"--run-id {run_id!r} is not a safe path segment (expected {_SAFE_RUN_ID.pattern}). "
            "It names a tarball and remote directories, so a separator or metacharacter in it "
            "would write outside the intended paths."
        )

    # Skipped in --no-publish (test) mode: a test publishes no artifact, so there
    # is nothing to cache against — it must run on every (re-)run.
    if not dryrun and not no_publish and s3_store.object_exists(artifact_name):
        print(f"submit-wait: artifact '{artifact_name}' already in the store — skipping build (cache hit).")
        write_outputs({"install-path": local_install_path, "cache-hit": "true"})
        return

    repo_script = Path(job_script)
    if not repo_script.is_file():
        raise CIError(f"--job-script does not exist: {repo_script}")

    site = load_site(site_name, config_path=troika_config, user=troika_user)
    if not dryrun:
        # A dry run only renders and asks troika to pretend, so any site will do;
        # a real run needs a scheduler to submit to and poll.
        ensure_batch_site(site, site_name)

    # Expand the work dir on the cluster before anything derives a path from it:
    # the runner has to scp into these, so they must be literal by the time troika
    # (which quotes its argv) sees them.
    resolved_work_dir = _resolve_reported("submit-wait", site._connection, remote_work_dir)
    paths = RemotePaths.derive(resolved_work_dir, artifact_name)

    ships_source = bool(source_dir and run_id)
    # Dependency prefixes are runner-local; when we ship the source we also ship
    # them onto the shared FS and point the job's CMAKE_PREFIX_PATH at the cluster
    # copies. With no source shipping (dry run), the prefix is passed through as-is.
    if ships_source:
        remote_cmake_prefix, local_prefixes, remote_deps_dir = plan_remote_prefixes(cmake_prefix_path, paths.staging)
    else:
        remote_cmake_prefix, local_prefixes, remote_deps_dir = cmake_prefix_path, [], ""
    # The scheduler-side identity a re-run reattaches by (see submit_or_reattach).
    job_name = jobscript.job_name_for(artifact_name)
    rendered = jobscript.render_job_script(
        repo_script=repo_script.read_text(),
        output_path=paths.output,
        cmake_prefix_path=remote_cmake_prefix,
        install_path=paths.install,
        job_name=job_name,
        staging_dir=paths.staging if ships_source else None,
        run_id=run_id if ships_source else None,
        marker_wait_timeout=marker_wait_timeout,
    )
    # Render next to the repo script so troika's copy_script picks it up locally.
    script_path = repo_script.parent / f"job-{artifact_name}.sh"
    write_job_script(script_path, rendered)

    output = paths.output
    install_path = paths.install
    staging_dir = paths.staging

    # Submit-then-poll: submit first (claim the queue slot), then ship the source
    # and drop the marker the job is waiting on. after_submit runs only on a
    # fresh submit; a reattach is handled below (re-ship only if its marker is
    # missing) so an in-flight job's sources are never disturbed.
    def ship_for(this_run_id: str) -> None:
        """Stage the source and dep prefixes, unless someone already has.

        The skip is not an optimisation, it is a safety property. Two runs can
        want the same artifact at once (a repo's own CI and a fan-out), and the
        loser of the submit race still reaches this. ``ship_source`` starts by
        RESETTING the staging dir — it renames the tree aside — so a second ship
        deletes ``deps/<i>`` out from under a job that is already reading them.
        That is not hypothetical: it took out an ecflow hpc-atos-nvidia job with

            CMake Error at CMakeLists.txt:28 (find_package):
              Could not find a package configuration file provided by "ecbuild"

        while the first job, reading the same staging dir, had found ecbuild and
        compiled a thousand targets.

        Skipping is safe because staging is per-artifact and the artifact name
        embeds the source SHA and the deps hash: a completed transfer sitting in
        this staging dir is, by construction, the same source and the same deps
        we were about to write. A PARTIAL transfer leaves no marker, so this
        still ships (and resets) in the case the reset exists for.

        The marker alone only rules out a peer that already FINISHED. Two runs
        that start shipping together both see no marker, and the second one's
        reset then pulls the first one's staged tarballs out from under it — the
        eckit ``deps/1.tgz: Cannot open: No such file or directory`` unpack
        failure. So the ship runs under ``transfer.ship_lock``, and the marker is
        re-checked INSIDE it: the first shipper resets and stages alone, and the
        second acquires only once the first is done, sees the marker and skips.
        The check outside the lock stays as the fast path, so the common
        already-built case still costs one ``test -f`` and no lock.
        """
        if transfer.marker_exists(site._connection, staging_dir=staging_dir):
            print(
                f"submit-wait: staging for '{artifact_name}' is already complete "
                "(another run shipped it); not re-shipping."
            )
            return
        with transfer.ship_lock(site._connection, staging_dir=staging_dir, run_id=this_run_id):
            if transfer.marker_exists(site._connection, staging_dir=staging_dir):
                print(
                    f"submit-wait: staging for '{artifact_name}' was completed by another run "
                    "while we waited for the staging lock; not re-shipping."
                )
                return
            transfer.ship_source(
                site._connection,
                local_source_dir=source_dir,
                staging_dir=staging_dir,
                run_id=this_run_id,
                tar_dir=tar_dir,
                local_prefixes=local_prefixes,
                remote_deps_dir=remote_deps_dir,
            )

    jid, action = submit_or_reattach(
        site=site,
        script_path=script_path,
        user=troika_user,
        output=output,
        job_name=job_name,
        after_submit=(lambda: ship_for(run_id)) if ships_source else None,
        dryrun=dryrun,
    )
    if dryrun:
        print(f"submit-wait: dry run complete; rendered {script_path} (no job submitted).")
        return

    if action == "reattached" and ships_source:
        # The reattached job waits for a marker in its staging dir, not for one
        # named after whoever submitted it — so we can answer "has anyone finished
        # shipping?" without knowing that run at all. If nothing has (submit
        # succeeded but the runner died before the ship), re-ship under OUR run id
        # so the waiting job can proceed.
        if not transfer.marker_exists(site._connection, staging_dir=staging_dir):
            print(f"submit-wait: reattached job {jid} has no transfer marker; re-shipping source.")
            ship_for(run_id)

    # From here a GitHub cancellation must scancel the batch job, not orphan it.
    _install_cancel_handler(site, script_path, output, jid)

    print(f"submit-wait: {action} job {jid} for '{artifact_name}' on site '{site_name}'. Waiting for completion...")
    # Stream the job's output live so the compiler/ctest logs show up as they are
    # produced; the sentinel waiter greps past them, so without this the step
    # would surface none of the actual build. Verdict logic is unchanged.
    print(f"submit-wait: --- live job output ({output}) ---")
    streamer = _stream_job_output(site._connection, output)
    try:
        verdict = wait_for_job(
            sentinel_waiter=_remote_sentinel_waiter(site._connection, output),
            state_getter=lambda: site._get_state(jid, strict=False),
        )
    finally:
        _stop_stream(streamer)
    print("submit-wait: --- end live job output ---")

    # A clean finish (SUCCESS/FAILURE) prints its sentinel, so the streamer has
    # already flushed the whole log and quit. A job that vanished or timed out
    # never printed one, so dump whatever the output holds to be sure the failure
    # is visible.
    if verdict in ("VANISHED", "TIMEOUT"):
        _echo_remote_output(site._connection, output)

    if verdict == "SUCCESS":
        if no_publish:
            # Test-only leg: the green sentinel is the whole result. There is no
            # install tree to fetch and no artifact to publish (the action skips
            # its Publish step); a fetch here would fail on the never-created
            # remote install dir.
            print(f"submit-wait: job {jid} finished successfully (test-only; nothing to publish).")
            return
        print(f"submit-wait: job {jid} finished successfully. Fetching install tree...")
        # Pull the tree the compute node built back to the runner for publishing.
        transfer.fetch_install(
            site._connection,
            remote_install_dir=install_path,
            local_install_dir=local_install_path,
            tar_dir=tar_dir,
        )
        write_outputs({"install-path": local_install_path, "cache-hit": "false"})
        return

    detail = {
        "FAILURE": "the build reported a failure",
        "VANISHED": "the job left the scheduler without reporting success (killed / cancelled / node failure)",
        "TIMEOUT": "the wait timed out",
    }[verdict]
    raise CIError(f"HPC job {jid} for '{artifact_name}' did not succeed: {detail}. Job output: {output}")


@main.command("cancel", help="Cancel the active job for an artifact (used on workflow cancellation).")
@_site_options
@click.option("--artifact-name", "artifact_name", required=True, help="Artifact whose job should be cancelled")
@click.option("--output", "output", required=True, help="Absolute job output path on the cluster")
def cancel(
    site_name: str,
    troika_config: str | None,
    troika_user: str | None,
    artifact_name: str,
    output: str,
) -> None:
    site = load_site(site_name, config_path=troika_config, user=troika_user)
    # Find the job through the scheduler by its artifact name (cross-runner), the
    # same lookup submit-wait reattaches by.
    found = find_active_job_by_name(site._connection, job_name=jobscript.job_name_for(artifact_name), user=troika_user)
    if found is None:
        print(f"cancel: no active job for '{artifact_name}'; nothing to cancel.")
        return
    jid = found
    # The job script path isn't needed to cancel by explicit jid, but troika's
    # kill() signature takes it; a placeholder next to nothing is fine since jid
    # is passed explicitly.
    _, status = cancel_job(
        site=site,
        script_path=Path(f"job-{artifact_name}.sh"),
        output=output,
        jid=jid,
    )
    print(f"cancel: requested cancellation of job {jid} for '{artifact_name}' (status: {status}).")


@main.command("gc", help="Remove per-artifact trees under the cluster work dir older than N days.")
@_site_options
@click.option(
    "--remote-work-dir",
    "remote_work_dir",
    required=True,
    help="Cluster work dir to sweep. May name cluster variables (e.g. '$SCRATCH/github-ci').",
)
@click.option("--older-than-days", "older_than_days", type=int, default=7, help="Age threshold in days (default: 7)")
@click.option("--dryrun", is_flag=True, default=False, help="List what would be removed; delete nothing")
def gc(
    site_name: str,
    troika_config: str | None,
    troika_user: str | None,
    remote_work_dir: str,
    older_than_days: int,
    dryrun: bool,
) -> None:
    site = load_site(site_name, config_path=troika_config, user=troika_user)
    resolved = resolve_remote_path(site._connection, remote_work_dir)
    run_gc(site._connection, remote_work_dir=resolved, older_than_days=older_than_days, dryrun=dryrun)


def _require_nested_remote_path(command: str, remote_dir: str, resolved: str) -> None:
    """Refuse a resolved cluster path that sits directly under root.

    Two failure modes share this guard, on remove-tree and push-tree. For
    remove-tree it stops a bare ``$SCRATCH`` or ``/`` from being wiped. For
    push-tree it catches the misconfiguration that produces such a path in the
    first place: an unset ``vars.HPC_CI_REMOTE_WORK_DIR`` interpolated into
    ``${{ vars.HPC_CI_REMOTE_WORK_DIR }}/transfer-e2e-<id>`` renders
    ``/transfer-e2e-<id>``, and the cluster then reports the confusing
    ``mkdir: cannot create directory '/transfer-e2e-...': Read-only file system``.
    Naming the variable here turns that into a one-line diagnosis.

    fetch-tree is deliberately NOT guarded: it only reads, and a shallow source
    such as ``/data`` is defensible on some clusters. In the round-trip flow its
    path comes from push-tree's output anyway, so it is already covered.
    """
    if resolved.strip("/").count("/") >= 1:
        return
    detail = f"{resolved!r}" if resolved == remote_dir else f"{resolved!r} (from {remote_dir!r})"
    raise CIError(
        f"{command}: refusing a top-level cluster path: {detail}. A path directly under / is almost "
        "always an unset work-dir variable — set vars.HPC_CI_REMOTE_WORK_DIR (e.g. '$SCRATCH/github-ci') "
        "so the path lands under it."
    )


@main.command("fetch-tree", help="Copy a directory a job produced off the cluster back to the runner.")
@_site_options
@click.option(
    "--remote-dir",
    "remote_dir",
    required=True,
    help="Source directory on the cluster to fetch. May name cluster variables (e.g. '$SCRATCH/ref'); "
    "expanded on the cluster, not on the runner.",
)
@click.option("--local-dir", "local_dir", required=True, help="Runner-local directory to unpack the tree into")
@click.option("--tar-dir", "tar_dir", required=True, help="Runner-local scratch dir for the transferred tarball")
@click.option("--dryrun", is_flag=True, default=False, help="Resolve the remote path but transfer nothing")
def fetch_tree_cmd(
    site_name: str,
    troika_config: str | None,
    troika_user: str | None,
    remote_dir: str,
    local_dir: str,
    tar_dir: str,
    dryrun: bool,
) -> None:
    site = load_site(site_name, config_path=troika_config, user=troika_user)
    resolved = _resolve_reported("fetch-tree", site._connection, remote_dir)  # the source lives on the cluster
    transfer.fetch_tree(site._connection, remote_dir=resolved, local_dir=local_dir, tar_dir=tar_dir, dryrun=dryrun)
    if dryrun:
        print(f"fetch-tree: dry run; would fetch {resolved} -> {local_dir}")
        return
    write_outputs({"local-dir": local_dir})
    print(f"fetch-tree: fetched {resolved} -> {local_dir}")


@main.command("push-tree", help="Copy a runner-local directory up to a directory on the cluster.")
@_site_options
@click.option("--local-dir", "local_dir", required=True, help="Source directory on the runner to push")
@click.option(
    "--remote-dir",
    "remote_dir",
    required=True,
    help="Destination directory on the cluster. May name cluster variables (e.g. '$SCRATCH/inputs'); "
    "expanded on the cluster, not on the runner.",
)
@click.option("--tar-dir", "tar_dir", required=True, help="Runner-local scratch dir for the transferred tarball")
@click.option("--dryrun", is_flag=True, default=False, help="Resolve the remote path but transfer nothing")
def push_tree_cmd(
    site_name: str,
    troika_config: str | None,
    troika_user: str | None,
    local_dir: str,
    remote_dir: str,
    tar_dir: str,
    dryrun: bool,
) -> None:
    site = load_site(site_name, config_path=troika_config, user=troika_user)
    resolved = _resolve_reported("push-tree", site._connection, remote_dir)  # the destination lives on the cluster
    _require_nested_remote_path("push-tree", remote_dir, resolved)
    transfer.push_tree(site._connection, local_dir=local_dir, remote_dir=resolved, tar_dir=tar_dir, dryrun=dryrun)
    if dryrun:
        print(f"push-tree: dry run; would push {local_dir} -> {resolved}")
        return
    write_outputs({"remote-dir": resolved})
    print(f"push-tree: pushed {local_dir} -> {resolved}")


@main.command(
    "remove-tree",
    help="Remove a directory a job left on the cluster (call on success to reclaim scratch).",
)
@_site_options
@click.option(
    "--remote-dir",
    "remote_dir",
    required=True,
    help="Directory on the cluster to remove. May name cluster variables (e.g. '$SCRATCH/out'); "
    "expanded on the cluster, not on the runner.",
)
@click.option("--dryrun", is_flag=True, default=False, help="Resolve the remote path but remove nothing")
def remove_tree_cmd(
    site_name: str,
    troika_config: str | None,
    troika_user: str | None,
    remote_dir: str,
    dryrun: bool,
) -> None:
    site = load_site(site_name, config_path=troika_config, user=troika_user)
    resolved = _resolve_reported("remove-tree", site._connection, remote_dir)
    _require_nested_remote_path("remove-tree", remote_dir, resolved)
    transfer.remove_tree(site._connection, remote_dir=resolved, dryrun=dryrun)
    if dryrun:
        print(f"remove-tree: dry run; would remove {resolved}")
        return
    print(f"remove-tree: removed {resolved}")


if __name__ == "__main__":
    main()
