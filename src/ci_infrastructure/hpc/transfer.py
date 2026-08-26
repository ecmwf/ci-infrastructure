# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Moving trees between the runner and the cluster over troika's connection.

The HPC backend treats the cluster as a stateless compute backend: nothing is
assumed to be visible on both sides. The runner owns the checkout and the S3
artifact store; the cluster only compiles. So two tree transfers bracket the
job — both driven over the same troika connection troika already uses for
submit/tail, so they need no extra transport:

  * :func:`ship_source` (submit-then-poll) tars the runner's checkout, scp's the
    tarball into the shared staging dir and finally ``touch``es the
    ``TRANSFER_COMPLETED`` marker; the already-submitted job blocks on
    that marker, then unpacks the tarball itself into node-local ``$TMPDIR``. The
    marker is dropped **last** so the job never sees a half-copied tarball;
  * :func:`fetch_install` tars the install tree the job produced on the cluster,
    scp's it back and unpacks it into a local staging directory (which the
    runner-side publish step then uploads to S3).

Trees move as a single ``.tgz`` because troika's connection only transfers one
file at a time; tar/untar on each side turns that into a directory copy.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .._errors import CIError
from . import jobscript


class Connection(Protocol):
    """The subset of troika's connection API the transfers rely on."""

    def execute(self, command: Any, stdout: Any = ..., stderr: Any = ..., dryrun: bool = ...) -> Any: ...

    def sendfile(self, src: Any, dst: Any, dryrun: bool = ...) -> None: ...

    def getfile(self, src: Any, dst: Any, dryrun: bool = ...) -> None: ...


def _run_remote(conn: Connection, argv: list[str], *, what: str, dryrun: bool = False) -> None:
    """Run a command on the remote and raise CIError on a non-zero exit."""
    proc = conn.execute(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, dryrun=dryrun)
    if dryrun:
        return
    _stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr).strip()
        raise CIError(f"{what} failed (exit {proc.returncode}): {detail}")


def _probe_remote(conn: Connection, argv: list[str]) -> int:
    """Run a command on the remote and return its exit code instead of raising.

    For remote tests whose *failure* is an answer rather than an error — "is the
    marker there?", "did the lock mkdir lose the race?".
    """
    proc = conn.execute(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.communicate()
    return int(proc.returncode)


def touch_remote_file(conn: Connection, *, path: str) -> None:
    """Create ``path`` (and its parent) on the remote if it does not exist."""
    parent = str(PurePosixPath(path).parent)
    _run_remote(conn, ["mkdir", "-p", parent], what=f"Remote mkdir of {parent}")
    _run_remote(conn, ["touch", path], what=f"Remote touch of {path}")


def _marker_path(staging_dir: str) -> str:
    return str(PurePosixPath(staging_dir) / jobscript.TRANSFER_MARKER_NAME)


def marker_exists(conn: Connection, *, staging_dir: str) -> bool:
    """Whether a completed source transfer is present in the artifact's staging dir.

    Used on reattach: if a job was submitted but the runner died before the scp
    or the marker, the still-waiting job needs the source (re-)shipped.

    Keyed by the staging dir alone. That dir is already per-artifact, which is
    exactly the scope of the question being asked ("did anyone finish shipping
    for this job?"), so a reattaching runner does not need to know which run
    submitted the job it adopted — and therefore never has to read the
    scheduler's ``Comment``, which sites rewrite.

    ROLLOVER SHIM: also true for a legacy per-run ``TRANSFER_COMPLETED_<run_id>``,
    so a job submitted by a pre-fix runner (whose source IS already staged) is not
    needlessly re-shipped over. Drop the second test once no pre-fix job can still
    be in flight.
    """
    # The directory is quoted, the trailing `*` is NOT: it has to reach the shell
    # unquoted to expand. Quoting the whole pattern would look for a file literally
    # named "TRANSFER_COMPLETED_*".
    staging_q = shlex.quote(str(PurePosixPath(staging_dir)))
    probe = (
        f"test -f {shlex.quote(_marker_path(staging_dir))} "
        f"|| ls {staging_q}/{jobscript.TRANSFER_MARKER_NAME}_* >/dev/null 2>&1"
    )
    proc = conn.execute(["sh", "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.communicate()
    return bool(proc.returncode == 0)


#: The ship lock is a SIBLING of the staging dir, never a child: ``ship_source``
#: starts by renaming the whole staging tree aside, which would carry a lock
#: living inside it away with the tree it is meant to protect.
SHIP_LOCK_SUFFIX = ".shiplock"
#: How long a shipper waits for a peer's lock before giving up. Comfortably under
#: the job's own ``DEFAULT_MARKER_WAIT_TIMEOUT`` (1800s), so a runner that cannot
#: get the lock fails with a lock error rather than leaving the job to time out on
#: a marker that is never coming.
DEFAULT_SHIP_LOCK_TIMEOUT = 900
#: Poll interval while waiting for a peer to release.
SHIP_LOCK_POLL_SECONDS = 10
#: A lock directory older than this is assumed abandoned (its runner died mid-ship)
#: and is broken by the next shipper. Longer than any healthy ship, which is a tar
#: + scp of a checkout and a handful of install trees.
SHIP_LOCK_STALE_MINUTES = 30


def _ship_lock_path(staging_dir: str) -> str:
    return f"{staging_dir.rstrip('/')}{SHIP_LOCK_SUFFIX}"


def _try_acquire_ship_lock(conn: Connection, *, lock_dir: str, run_id: str, stale_minutes: int) -> bool:
    """One attempt at claiming ``lock_dir``, breaking it if it is stale.

    ``mkdir`` of a single directory is the atomic test-and-set: it succeeds for
    exactly one caller and fails with ``EEXIST`` for the rest, on every filesystem
    the cluster exports (unlike ``O_EXCL`` opens or flock over NFS/Lustre). The
    owner file is written *inside* the new directory, so the directory's mtime is
    the acquisition time and never gets refreshed — which is what makes the
    staleness test below mean "held since", not "touched at".

    The lock's own ``mkdir`` must stay non-``-p`` (``-p`` succeeds on an existing
    directory, which is exactly the test being made), so its PARENT is created
    separately first: on a fresh work dir nothing has made ``<work>/staging`` yet
    — ``_reset_staging_dir`` does, but that runs *inside* this lock — and every
    attempt would otherwise fail ``ENOENT`` until the wait timed out.
    """
    q_lock = shlex.quote(lock_dir)
    q_owner = shlex.quote(f"{lock_dir}/owner")
    q_parent = shlex.quote(str(PurePosixPath(lock_dir).parent))
    claim = f"mkdir {q_lock} 2>/dev/null && {{ echo {shlex.quote(run_id)} > {q_owner} 2>/dev/null || true; }}"
    # Newline-joined, not "; "-joined: a multi-line `if ... then` needs real line
    # breaks — `then; rm ...` is a bash syntax error, and one that only shows up
    # when the script is actually run.
    script = "\n".join(
        [
            f"mkdir -p {q_parent} 2>/dev/null || true",
            f"if {claim}; then exit 0; fi",
            # Not ours. Break it only if nobody can plausibly still be shipping
            # under it, then race for it again like everyone else.
            f'if [ -n "$(find {q_lock} -maxdepth 0 -mmin +{stale_minutes} 2>/dev/null)" ]; then',
            f"  rm -rf {q_lock} 2>/dev/null || true",
            f"  if {claim}; then exit 0; fi",
            "fi",
            "exit 1",
        ]
    )
    return _probe_remote(conn, ["bash", "-c", script]) == 0


@contextmanager
def ship_lock(
    conn: Connection,
    *,
    staging_dir: str,
    run_id: str,
    timeout: int = DEFAULT_SHIP_LOCK_TIMEOUT,
    poll: int = SHIP_LOCK_POLL_SECONDS,
    stale_minutes: int = SHIP_LOCK_STALE_MINUTES,
    dryrun: bool = False,
) -> Iterator[None]:
    """Hold an exclusive cluster-wide claim on an artifact's staging dir while shipping into it.

    Staging is keyed on the artifact alone, so two runs that both want the same
    artifact (a repo's own CI and a fan-out, or two fan-outs from sibling PRs)
    ship into ONE directory. The marker check in the caller only rules out a peer
    that already *finished*; two shippers that start together both find no marker
    and both proceed, and then :func:`ship_source`'s reset — which renames the
    staging tree aside — deletes the peer's already-staged files out from under
    it. The peer's very next command is a remote untar of a tarball that no longer
    exists, which is the observed::

        Remote tree unpack failed (exit 2): tar (child): <staging>/deps/1.tgz:
          Cannot open: No such file or directory

    Serialising the shippers is what makes the reset safe: only one resets at a
    time, and whoever gets the lock second re-checks the marker and finds the
    first one's completed transfer, so it skips instead of overwriting it.

    Held across the whole ship (reset -> source -> deps -> marker) and released in
    a ``finally``, best-effort, so a failed release cannot mask the real error;
    an abandoned lock is broken after ``stale_minutes`` by the next shipper.
    """
    if dryrun:
        yield
        return
    lock_dir = _ship_lock_path(staging_dir)
    deadline = time.monotonic() + timeout
    waited = False
    while not _try_acquire_ship_lock(conn, lock_dir=lock_dir, run_id=run_id, stale_minutes=stale_minutes):
        if time.monotonic() >= deadline:
            raise CIError(
                f"Timed out after {timeout}s waiting for the staging lock {lock_dir}. "
                "Another run is shipping this artifact; remove the lock directory if its runner is gone."
            )
        if not waited:
            waited = True
            print(f"ship: staging for '{staging_dir}' is locked by another run; waiting up to {timeout}s.")
        time.sleep(poll)
    try:
        yield
    finally:
        # Best-effort and always exit 0: a release that failed must not replace
        # the exception (if any) that is already propagating out of the ship.
        _run_remote(
            conn,
            ["bash", "-c", f"rm -rf {shlex.quote(lock_dir)} 2>/dev/null || true"],
            what=f"Release of staging lock {lock_dir}",
        )


def _reset_staging_dir(conn: Connection, *, staging_dir: str, run_id: str) -> None:
    """Reset the artifact's staging tree to empty, tolerating concurrent access.

    A bare ``rm -rf staging_dir`` is fragile here. Staging is shared per artifact,
    but job-id dedup is runner-local, so two runs building the same artifact can
    ship concurrently — and a still-running sibling job reads ``<staging>/deps``
    for the whole of its build. Either can repopulate a directory mid-delete, and
    on the parallel filesystem ``rm`` then fails its final ``rmdir`` with
    ``ENOTEMPTY`` (the reported "cannot remove '<...>/deps': Directory not
    empty"). Instead rename the old tree aside in a single metadata operation —
    which cannot hit ``ENOTEMPTY`` — and delete the moved-aside copy best-effort,
    so cleanup never fails the ship.

    That makes the reset survivable, NOT concurrency-safe: renaming the tree aside
    still takes a peer shipper's already-staged files with it, and the peer's next
    remote untar then fails on a tarball that no longer exists. Callers must
    therefore reset only while holding :func:`ship_lock`, which is what actually
    makes "one resetter at a time" true.

    The trailing ``mkdir -p`` is the script's last command, so its exit status is
    the one ``_run_remote`` checks: a staging dir we genuinely cannot create
    still fails loudly, while the best-effort rename/delete never do.
    """
    trash = f"{staging_dir.rstrip('/')}.trash.{run_id}"
    parent = str(PurePosixPath(staging_dir).parent)
    q_staging, q_trash, q_parent = (shlex.quote(p) for p in (staging_dir, trash, parent))
    reset = "; ".join(
        [
            f"mkdir -p {q_parent}",
            f"if [ -e {q_staging} ]; then mv {q_staging} {q_trash} 2>/dev/null || true; fi",
            f"rm -rf {q_trash} 2>/dev/null || true",
            f"mkdir -p {q_staging}",
        ]
    )
    _run_remote(conn, ["bash", "-c", reset], what="Staging reset")


def ship_source(
    conn: Connection,
    *,
    local_source_dir: str,
    staging_dir: str,
    run_id: str,
    tar_dir: str,
    local_prefixes: Sequence[str] = (),
    remote_deps_dir: str | None = None,
    dryrun: bool = False,
) -> None:
    """Tar the local checkout (and dep prefixes), scp them into ``staging_dir`` and drop the marker.

    The job (already submitted) unpacks the source tarball itself. The dependency
    install trees named in ``local_prefixes`` are runner-local — the compute node
    cannot see them — so each is tarred, shipped and unpacked *here* into
    ``<remote_deps_dir>/<i>`` on the shared filesystem, matching the cluster
    ``CMAKE_PREFIX_PATH`` the orchestrator baked into the job. The staging dir is
    cleared first so a prior attempt's tarball / stale ``TRANSFER_COMPLETED_*``
    markers can't be mistaken for this one. The marker is dropped **last**, after
    every input is fully staged, so the job never starts against a partial copy.
    """
    if dryrun:
        return
    remote_tgz = str(PurePosixPath(staging_dir) / jobscript.SOURCE_TARBALL_NAME)
    local_tgz = Path(tar_dir) / f"{run_id}.src.tgz"

    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-czf", str(local_tgz), "-C", str(local_source_dir), "."], check=True)
    _reset_staging_dir(conn, staging_dir=staging_dir, run_id=run_id)
    conn.sendfile(local_tgz, remote_tgz)
    # Ship each dependency prefix onto the shared FS before the marker, so the
    # compute node's CMAKE_PREFIX_PATH resolves to trees it can actually read.
    if remote_deps_dir is not None:
        for index, prefix in enumerate(local_prefixes):
            push_tree(
                conn,
                local_dir=prefix,
                remote_dir=f"{remote_deps_dir.rstrip('/')}/{index}",
                tar_dir=tar_dir,
                tarball_suffix="",
                local_tar_name=f"{run_id}.dep{index}.tgz",
            )
    # Marker last: the job must never see it before every input is fully staged.
    _run_remote(conn, ["touch", _marker_path(staging_dir)], what="Transfer-complete marker")


def fetch_tree(
    conn: Connection,
    *,
    remote_dir: str,
    local_dir: str,
    tar_dir: str,
    tarball_suffix: str = "fetch",
    dryrun: bool = False,
) -> None:
    """Tar a directory on the cluster and unpack it into ``local_dir`` on the runner.

    The generic HPC->runner half of the two bracketing transfers, driven straight
    over troika's connection (no scheduler, so it works against ``direct`` sites
    too). The tree moves as a single ``<name>.<tarball_suffix>.tgz`` next to the
    source on the cluster, because troika's connection transfers one file at a
    time; tar/untar on each side turns that into a directory copy.
    """
    if dryrun:
        return
    remote = PurePosixPath(remote_dir)
    remote_tgz = str(remote.parent / f"{remote.name}.{tarball_suffix}.tgz")
    local_tgz = Path(tar_dir) / f"{remote.name}.{tarball_suffix}.tgz"

    _run_remote(
        conn,
        ["bash", "-c", f"tar -czf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_dir)} ."],
        what="Remote tree tar",
    )
    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    conn.getfile(remote_tgz, local_tgz)
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-xzf", str(local_tgz), "-C", str(local_dir)], check=True)


def push_tree(
    conn: Connection,
    *,
    local_dir: str,
    remote_dir: str,
    tar_dir: str,
    tarball_suffix: str = "push",
    local_tar_name: str | None = None,
    dryrun: bool = False,
) -> None:
    """Tar ``local_dir`` on the runner and unpack it into ``remote_dir`` on the cluster.

    The generic runner->HPC mirror of :func:`fetch_tree`: local tar -> ``sendfile``
    -> remote ``mkdir -p`` + untar. Like ``fetch_tree`` it needs no scheduler, so
    it works against ``direct`` sites too.

    ``local_tar_name`` overrides the runner-side scratch tarball's name and an
    empty ``tarball_suffix`` gives a bare ``<dir>.tgz`` on the cluster. The
    source-shipping flow uses both: its per-dep tarballs are named after the run
    (``<run_id>.dep<i>.tgz``) so concurrent runs sharing ``tar_dir`` cannot
    overwrite each other's, and their cluster-side name has no suffix.
    """
    if dryrun:
        return
    name = PurePosixPath(remote_dir).name
    # An empty suffix means a bare `<dir>.tgz` — no doubled separator.
    ext = f".{tarball_suffix}.tgz" if tarball_suffix else ".tgz"
    local_tgz = Path(tar_dir) / (local_tar_name or f"{name}{ext}")
    remote_tgz = f"{remote_dir.rstrip('/')}{ext}"

    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-czf", str(local_tgz), "-C", str(local_dir), "."], check=True)
    _run_remote(conn, ["mkdir", "-p", remote_dir], what=f"Remote mkdir of {remote_dir}")
    conn.sendfile(local_tgz, remote_tgz)
    _run_remote(
        conn,
        ["bash", "-c", f"tar -xzf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_dir)}"],
        what="Remote tree unpack",
    )


def remove_tree(conn: Connection, *, remote_dir: str, dryrun: bool = False) -> None:
    """Remove a directory (and any leftover transfer tarballs) on the cluster.

    The reclaim companion to :func:`push_tree` / :func:`fetch_tree`: a single
    ``rm -rf`` of ``remote_dir`` plus its sibling ``<remote_dir>.push.tgz`` /
    ``<remote_dir>.fetch.tgz`` transfer tarballs (harmless no-ops when absent). Like
    the transfers it needs no scheduler. The caller is responsible for guarding
    against a top-level ``remote_dir`` before calling.
    """
    if dryrun:
        return
    base = remote_dir.rstrip("/")
    _run_remote(
        conn,
        ["rm", "-rf", base, f"{base}.push.tgz", f"{base}.fetch.tgz"],
        what=f"Remote remove of {remote_dir}",
    )


def fetch_install(
    conn: Connection,
    *,
    remote_install_dir: str,
    local_install_dir: str,
    tar_dir: str,
    dryrun: bool = False,
) -> None:
    """Tar the cluster's install tree and unpack it into ``local_install_dir`` on the runner.

    Thin wrapper over :func:`fetch_tree` that keeps the build flow's
    ``<name>.install.tgz`` temp-tarball name.
    """
    fetch_tree(
        conn,
        remote_dir=remote_install_dir,
        local_dir=local_install_dir,
        tar_dir=tar_dir,
        tarball_suffix="install",
        dryrun=dryrun,
    )
