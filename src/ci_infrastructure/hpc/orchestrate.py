#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Submit / wait / cancel a SLURM build job via troika (as a library); used by ``build-on-hpc``.

``submit-wait`` is idempotent: a cache hit skips, an active job with the same
name is reattached, otherwise a fresh job is submitted. The output sentinel,
not ``squeue``, is the verdict; ``squeue`` is only a slow liveness guard.
"""

from __future__ import annotations

import json
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

ACTIVE_STATES: Final = frozenset(
    {"PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "RESIZING", "SUSPENDED", "REQUEUED"}
)

_DEFAULT_GUARD_INTERVAL: Final = 120
_DEFAULT_WAIT_TIMEOUT: Final = 6 * 60 * 60
_GRACE_SECONDS: Final = 10  # last look for a late-flushed sentinel after the job leaves the queue
_WAITER_GRACE_SECONDS: Final = 15  # slack over the remote timeout before we give up on the tail itself

Verdict = Literal["SUCCESS", "FAILURE", "VANISHED", "TIMEOUT"]


def _parse_matrix_leg(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        leg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CIError(f"--matrix-leg is not valid JSON: {exc}") from exc
    if not isinstance(leg, dict):
        raise CIError(f"--matrix-leg must be a JSON object (the matrix leg), got {type(leg).__name__}")
    return leg


def resolve_recipe(repo_script: Path, *, matrix_leg: str, artifact_name: str) -> str:
    source = repo_script.read_text()
    if not jobscript.is_job_template(repo_script):
        return source
    leg = _parse_matrix_leg(matrix_leg)
    if not leg:
        raise CIError(
            f"--job-script {repo_script} is a '{jobscript.JOB_TEMPLATE_SUFFIX}' template, but no "
            "--matrix-leg was passed, so there is nothing to render it against. The build-on-hpc "
            "action forwards it as `matrix-leg: ${{ toJSON(matrix) }}`; a hand-written caller must too."
        )
    try:
        return jobscript.render_job_template(
            template_source=source,
            template_name=str(repo_script),
            leg=leg,
            artifact_name=artifact_name,
            search_path=repo_script.parent,
        )
    except jobscript.JobTemplateError as exc:
        raise CIError(str(exc)) from exc


def write_job_script(path: Path, rendered: str) -> None:
    """Executable, so a leftover can be run by hand."""
    path.write_text(rendered)
    path.chmod(0o755)


class RemotePaths(NamedTuple):
    output: str
    install: str
    staging: str
    lock: str

    @classmethod
    def derive(cls, work_dir: str, artifact_name: str) -> RemotePaths:
        base = PurePosixPath(work_dir)
        return cls(
            output=str(base / "hpc-jobs" / f"{artifact_name}.out"),
            install=str(base / "install" / artifact_name),
            staging=str(base / "staging" / artifact_name),
            # Not under `staging`: a leg that ships no source still submits.
            lock=str(base / "locks" / artifact_name),
        )


def plan_remote_prefixes(cmake_prefix_path: str, staging_dir: str) -> tuple[str, list[str], str]:
    """Map runner-local dep prefixes to ``<staging_dir>/deps/<i>``: (cluster prefix path, local prefixes, deps dir)."""
    local_prefixes = [p for p in re.split(r"[;:]", cmake_prefix_path) if p]
    remote_deps_dir = f"{staging_dir.rstrip('/')}/deps"
    remote_prefixes = [f"{remote_deps_dir}/{index}" for index in range(len(local_prefixes))]
    return ":".join(remote_prefixes), local_prefixes, remote_deps_dir


def find_active_job_by_name(conn: Any, *, job_name: str, user: str | None) -> int | None:
    """Lowest active jid named ``job_name``, or None (also when ``squeue`` fails).

    Never the ``--comment``: sites rewrite it (ECMWF's sbatch appends ``;Gres=...``).
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
    """Reattach to an active job named ``job_name``, else submit and run ``after_submit``."""
    if not dryrun:
        found = find_active_job_by_name(site._connection, job_name=job_name, user=user)
        if found is not None:
            return found, "reattached"

    # troika's `create_output_dir` pre_submit hook only runs through its controller, which we bypass.
    site.create_output_dir(output, dryrun=dryrun)
    if not dryrun:
        transfer.truncate_remote_file(site._connection, path=output)

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
    """Block until the sentinel appears, the job leaves the queue, or ``timeout``."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "TIMEOUT"
        window = min(guard_interval * (1.0 + random.uniform(-jitter, jitter)), remaining)
        verdict = sentinel_waiter(window)
        if verdict is not None:
            return verdict
        if state_getter() is None:
            # The sentinel may still be flushing.
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
    return site.kill(str(script_path), None, output, jid=jid, dryrun=dryrun)


#: A run id becomes a path segment, so no separators or shell metacharacters.
_SAFE_RUN_ID: Final = re.compile(r"^[A-Za-z0-9._-]+$")


#: Swept at maxdepth 1 by age. "transfer-e2e" holds smoke-test-hpc.yml's trees.
GC_SUBDIRS: Final = ("staging", "install", "hpc-jobs", "locks", "transfer-e2e")


def run_gc(conn: Any, *, remote_work_dir: str, older_than_days: int, dryrun: bool = False) -> None:
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

    def handler(signum: int, frame: FrameType | None) -> None:
        print(f"submit-wait: received signal {signum}; cancelling HPC job {jid}...")
        try:
            cancel_job(site=site, script_path=script_path, output=output, jid=jid)
        except Exception as exc:  # best-effort: never mask the cancellation itself
            print(f"submit-wait: cancel of job {jid} failed: {exc}")
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _remote_sentinel_waiter(conn: Any, output: str, jid: int) -> Callable[[float], Verdict | None]:
    """Fails closed: only a sentinel naming ``jid`` counts.

    The window is bounded remotely: killing the local process would leave tail/grep alive.
    """
    pattern = jobscript.sentinel_regex(jid)
    quoted_output = shlex.quote(output)
    quoted_pattern = shlex.quote(pattern)

    def wait(seconds: float) -> Verdict | None:
        window = max(1, int(seconds))
        pipeline = f"timeout {window} tail -F -n +1 {quoted_output} 2>/dev/null | grep -m1 -E {quoted_pattern}"
        proc = conn.execute(["bash", "-c", pipeline], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            # Backstop for a wedged connection.
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


def _echo_remote_output(conn: Any, output: str) -> None:
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


def _stream_job_output(conn: Any, output: str, jid: int) -> Any:
    """Display only. troika sends ``stdout=None`` to /dev/null, so pass the runner's stdout."""
    ceiling = int(_DEFAULT_WAIT_TIMEOUT + _WAITER_GRACE_SECONDS)
    quoted_output = shlex.quote(output)
    sed_quit = f"/{jobscript.sentinel_regex(jid)}/q"
    # -E: in BRE the pattern's `(`, `|` and `)` are literals.
    pipeline = f"timeout {ceiling} tail -F -n +1 {quoted_output} 2>/dev/null | sed -E '{sed_quit}'"
    sys.stdout.flush()
    return conn.execute(["bash", "-c", pipeline], stdout=sys.stdout)


def _stop_stream(proc: Any) -> None:
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
    resolved = resolve_remote_path(conn, remote_dir)
    if resolved != remote_dir:
        print(f"{command}: remote dir {remote_dir!r} -> {resolved}")
    return resolved


@click.group(help="Submit / wait / cancel a SLURM build job via troika.")
def main() -> None:
    pass


@main.command("render", help="Render a .j2 job-script against a matrix leg and print it. No cluster needed.")
@click.option("--job-script", "job_script", required=True, help="Path to the repo's .ci/hpc/build-<toolchain>.sh[.j2]")
@click.option("--matrix-leg", "matrix_leg", default="", help="The matrix leg as JSON (the render context)")
@click.option("--artifact-name", "artifact_name", default="", help="Value for the template's `artifact_name`")
def render(job_script: str, matrix_leg: str, artifact_name: str) -> None:
    path = Path(job_script)
    if not path.is_file():
        raise CIError(f"--job-script does not exist: {path}")
    sys.stdout.write(resolve_recipe(path, matrix_leg=matrix_leg, artifact_name=artifact_name))


@main.command("submit-wait", help="Submit (or reattach to) a build job and wait for it to finish.")
@_site_options
@click.option("--job-script", "job_script", required=True, help="Path to the repo's .ci/hpc/build-<toolchain>.sh")
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
@click.option(
    "--matrix-leg",
    "matrix_leg",
    default="",
    help="The matrix leg as JSON; the render context for a .j2 job-script. Ignored for any other recipe.",
)
@click.option("--cmake-prefix-path", "cmake_prefix_path", default="", help="Resolved dependency prefixes")
@click.option("--dryrun", is_flag=True, default=False, help="Render + go through troika in dry-run mode; do not submit")
@click.option(
    "--no-publish",
    "no_publish",
    is_flag=True,
    default=False,
    help="Test-only: run the job for its pass/fail, skip the artifact cache check and fetch no install tree.",
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
    matrix_leg: str,
    cmake_prefix_path: str,
    dryrun: bool,
    no_publish: bool,
) -> None:
    if run_id and not _SAFE_RUN_ID.fullmatch(run_id):
        raise CIError(
            f"--run-id {run_id!r} is not a safe path segment (expected {_SAFE_RUN_ID.pattern}). "
            "It names a tarball and remote directories, so a separator or metacharacter in it "
            "would write outside the intended paths."
        )

    def cache_hit() -> bool:
        if dryrun or no_publish or not s3_store.object_exists(artifact_name):
            return False
        print(f"submit-wait: artifact '{artifact_name}' already in the store — skipping build (cache hit).")
        write_outputs({"install-path": local_install_path, "cache-hit": "true"})
        return True

    if cache_hit():
        return

    repo_script = Path(job_script)
    if not repo_script.is_file():
        raise CIError(f"--job-script does not exist: {repo_script}")
    # Before load_site: a template that cannot render costs no ssh round-trip.
    recipe = resolve_recipe(repo_script, matrix_leg=matrix_leg, artifact_name=artifact_name)

    site = load_site(site_name, config_path=troika_config, user=troika_user)
    if not dryrun:
        ensure_batch_site(site, site_name)

    resolved_work_dir = _resolve_reported("submit-wait", site._connection, remote_work_dir)
    paths = RemotePaths.derive(resolved_work_dir, artifact_name)

    ships_source = bool(source_dir and run_id)
    if ships_source:
        remote_cmake_prefix, local_prefixes, remote_deps_dir = plan_remote_prefixes(cmake_prefix_path, paths.staging)
    else:
        remote_cmake_prefix, local_prefixes, remote_deps_dir = cmake_prefix_path, [], ""
    job_name = jobscript.job_name_for(artifact_name)
    rendered = jobscript.render_job_script(
        repo_script=recipe,
        output_path=paths.output,
        cmake_prefix_path=remote_cmake_prefix,
        install_path=paths.install,
        job_name=job_name,
        staging_dir=paths.staging if ships_source else None,
        run_id=run_id if ships_source else None,
        marker_wait_timeout=marker_wait_timeout,
    )
    print(f"::group::Job script submitted for {artifact_name} (from {repo_script})")
    print(rendered)
    print("::endgroup::")
    # Render next to the repo script so troika's copy_script picks it up locally.
    script_path = repo_script.parent / f"job-{artifact_name}.sh"
    write_job_script(script_path, rendered)

    def shipped() -> bool:
        if not transfer.marker_exists(site._connection, staging_dir=paths.staging):
            return False
        print(f"submit-wait: staging for '{artifact_name}' is already complete; not re-shipping.")
        return True

    def ship_for(this_run_id: str) -> None:
        """Never reset a staging dir a running job reads; skipping is safe as the name embeds SHA and deps hash."""
        if shipped():
            return
        with transfer.ship_lock(site._connection, staging_dir=paths.staging, run_id=this_run_id):
            if shipped():
                return
            transfer.ship_source(
                site._connection,
                local_source_dir=source_dir,
                staging_dir=paths.staging,
                run_id=this_run_id,
                tar_dir=tar_dir,
                local_prefixes=local_prefixes,
                remote_deps_dir=remote_deps_dir,
            )

    # Serialise submit per artifact: the reattach check is check-then-act on a
    # shared scheduler. Lock order is always submit -> ship.
    with transfer.remote_lock(
        site._connection,
        lock_dir=paths.lock,
        run_id=run_id or artifact_name,
        what="submit",
        subject=artifact_name,
        dryrun=dryrun,
    ):
        # A peer may have published while we waited for the lock.
        if cache_hit():
            return

        jid, action = submit_or_reattach(
            site=site,
            script_path=script_path,
            user=troika_user,
            output=paths.output,
            job_name=job_name,
            after_submit=(lambda: ship_for(run_id)) if ships_source else None,
            dryrun=dryrun,
        )
    if dryrun:
        print(f"submit-wait: dry run complete; rendered {script_path} (no job submitted).")
        return

    if action == "reattached" and ships_source:
        # The submitting runner may have died before shipping.
        ship_for(run_id)

    _install_cancel_handler(site, script_path, paths.output, jid)

    print(f"submit-wait: {action} job {jid} for '{artifact_name}' on site '{site_name}'. Waiting for completion...")
    print(f"submit-wait: --- live job output ({paths.output}) ---")
    streamer = _stream_job_output(site._connection, paths.output, jid)
    try:
        verdict = wait_for_job(
            sentinel_waiter=_remote_sentinel_waiter(site._connection, paths.output, jid),
            state_getter=lambda: site._get_state(jid, strict=False),
        )
    finally:
        _stop_stream(streamer)
    print("submit-wait: --- end live job output ---")

    # Without a sentinel the streamer may not have shown everything.
    if verdict in ("VANISHED", "TIMEOUT"):
        _echo_remote_output(site._connection, paths.output)

    if verdict == "SUCCESS":
        if no_publish:
            print(f"submit-wait: job {jid} finished successfully (test-only; nothing to publish).")
            return
        print(f"submit-wait: job {jid} finished successfully. Fetching install tree...")
        transfer.fetch_install(
            site._connection,
            remote_install_dir=paths.install,
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
    raise CIError(f"HPC job {jid} for '{artifact_name}' did not succeed: {detail}. Job output: {paths.output}")


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
    found = find_active_job_by_name(site._connection, job_name=jobscript.job_name_for(artifact_name), user=troika_user)
    if found is None:
        print(f"cancel: no active job for '{artifact_name}'; nothing to cancel.")
        return
    # troika's kill() takes a script path; with an explicit jid a placeholder is fine.
    _, status = cancel_job(site=site, script_path=Path(f"job-{artifact_name}.sh"), output=output, jid=found)
    print(f"cancel: requested cancellation of job {found} for '{artifact_name}' (status: {status}).")


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
    """Refuse a path directly under / (usually an unset HPC_CI_REMOTE_WORK_DIR); fetch-tree only reads."""
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
    resolved = _resolve_reported("fetch-tree", site._connection, remote_dir)
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
    resolved = _resolve_reported("push-tree", site._connection, remote_dir)
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
