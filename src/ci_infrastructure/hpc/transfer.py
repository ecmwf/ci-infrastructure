# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Move trees between runner and cluster over troika's connection, one tarball each (it sends one file at a time)."""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from .._errors import CIError
from . import jobscript


class Connection(Protocol):
    def execute(self, command: Any, stdout: Any = ..., stderr: Any = ..., dryrun: bool = ...) -> Any: ...

    def sendfile(self, src: Any, dst: Any, dryrun: bool = ...) -> None: ...

    def getfile(self, src: Any, dst: Any, dryrun: bool = ...) -> None: ...


def _run_remote(conn: Connection, argv: list[str], *, what: str, dryrun: bool = False) -> None:
    proc = conn.execute(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, dryrun=dryrun)
    if dryrun:
        return
    _stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr).strip()
        raise CIError(f"{what} failed (exit {proc.returncode}): {detail}")


def _probe_remote(conn: Connection, argv: list[str]) -> int:
    proc = conn.execute(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.communicate()
    return int(proc.returncode)


def truncate_remote_file(conn: Connection, *, path: str) -> None:
    """Create or empty ``path``: it is per-artifact, so a re-run would otherwise read the previous sentinel."""
    parent = str(PurePosixPath(path).parent)
    _run_remote(conn, ["mkdir", "-p", parent], what=f"Remote mkdir of {parent}")
    _run_remote(conn, ["sh", "-c", f": > {shlex.quote(path)}"], what=f"Remote truncate of {path}")


_SAFE_DEP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def remote_dep_names(local_prefixes: Sequence[str]) -> list[str]:
    """Each prefix's directory name on the cluster: its own name, as on the runner, so a
    library's relative RPATH ($ORIGIN/../../<package>/lib) resolves the same on both.
    The index when names collide or are unsafe."""
    names = [PurePosixPath(p.rstrip("/")).name for p in local_prefixes]
    if len(set(names)) == len(names) and all(_SAFE_DEP_NAME.fullmatch(n) for n in names):
        return names
    return [str(i) for i in range(len(local_prefixes))]


def _marker_path(staging_dir: str) -> str:
    return str(PurePosixPath(staging_dir) / jobscript.TRANSFER_MARKER_NAME)


def marker_exists(conn: Connection, *, staging_dir: str) -> bool:
    return _probe_remote(conn, ["test", "-f", _marker_path(staging_dir)]) == 0


#: A sibling of the staging dir, never a child: the reset renames the staging tree aside.
SHIP_LOCK_SUFFIX: Final = ".shiplock"
#: Under the job's ``DEFAULT_MARKER_WAIT_TIMEOUT``, so a lock timeout surfaces first.
DEFAULT_SHIP_LOCK_TIMEOUT: Final = 900
SHIP_LOCK_POLL_SECONDS: Final = 10
#: A lock older than this is assumed abandoned and broken by the next shipper.
SHIP_LOCK_STALE_MINUTES: Final = 30


def _ship_lock_path(staging_dir: str) -> str:
    return f"{staging_dir.rstrip('/')}{SHIP_LOCK_SUFFIX}"


#: The claim's exit code when `mkdir` failed and left no lock: nobody holds it, the
#: filesystem refused (quota, permissions), and waiting would only time out.
_LOCK_UNCREATABLE: Final = 2

#: The file in a lock directory naming who holds it (one line of JSON).
LOCK_OWNER_FILE: Final = "owner"


def lock_owner(run_id: str, *, env: Mapping[str, str] | None = None) -> str:
    """Who claims a lock, as one line of JSON: the run, its repo and job, this host and process, and when.

    Unique per claim (host, pid, time), so a run's later attempt never mistakes an earlier claim for its own.
    """
    e = os.environ if env is None else env
    server, repo, gh_run = e.get("GITHUB_SERVER_URL", ""), e.get("GITHUB_REPOSITORY", ""), e.get("GITHUB_RUN_ID", "")
    owner = {
        "run_id": run_id,
        "repository": repo,
        "run_url": f"{server}/{repo}/actions/runs/{gh_run}" if server and repo and gh_run else "",
        "job": e.get("GITHUB_JOB", ""),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "acquired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    return json.dumps(owner, sort_keys=True, separators=(",", ":"))


def _owner_is(lock_dir: str, owner: str) -> str:
    """A bash test: the lock's owner file says exactly ``owner``."""
    return f'[ "$(cat {shlex.quote(f"{lock_dir}/{LOCK_OWNER_FILE}")} 2>/dev/null)" = {shlex.quote(owner)} ]'


def _decode(data: Any) -> str:
    return data.decode(errors="replace").strip() if isinstance(data, bytes) else str(data or "").strip()


#: The sub-lock that lets one waiter at a time break a stale lock: ``<lock><suffix>``.
LOCK_BREAKING_SUFFIX: Final = ".breaking"
#: No-op lines of the acquire script where another waiter may act meanwhile; tests inject there.
LOCK_SEAM_SEEN: Final = ": lock-seam seen-stale"
LOCK_SEAM_RECHECKED: Final = ": lock-seam rechecked"
LOCK_SEAM_RENAMED: Final = ": lock-seam renamed"


def _try_acquire_lock(
    conn: Connection, *, lock_dir: str, run_id: str, stale_minutes: int, owner: str | None = None
) -> bool:
    """One atomic ``mkdir`` (no ``-p``) claim, then the owner file.

    A lock unchanged for over ``stale_minutes`` (a holder with a heartbeat keeps refreshing it, see
    ``remote_lock``) is broken, one waiter at a time: the breaker first takes the sub-lock
    ``<lock>.breaking`` (``mkdir``; a waiter that finds it held tries again on its next poll; one
    left by a dead breaker is cleared by the same stale rule), then looks again at the lock *as it
    is now* and renames it aside only while it is still stale. The renamed dir is removed only when
    its owner file is the one read before the rename and it is still stale; anything else is
    renamed back with ``mv -T -n`` (never over a lock claimed meanwhile) or, failing that, left
    where it is with a warning, never deleted. What is broken, and whose it was, is printed.

    Residual race: two waiters that both find a dead breaker's ``.breaking`` stale may each clear
    it and break at once. That needs a breaker to die inside the break and two waiters to pause for
    minutes inside a few lines of shell; even then the owner and staleness check after the rename
    keeps a re-claimed lock from ever being deleted (at worst it is left aside, with a warning).
    """
    owner = owner if owner is not None else lock_owner(run_id)
    q_lock = shlex.quote(lock_dir)
    q_owner_file = shlex.quote(f"{lock_dir}/{LOCK_OWNER_FILE}")
    q_breaking = shlex.quote(f"{lock_dir}{LOCK_BREAKING_SUFFIX}")
    q_parent = shlex.quote(str(PurePosixPath(lock_dir).parent))
    claim = f"{{ printf '%s\\n' {shlex.quote(owner)} > {q_owner_file} 2>/dev/null || true; }}"
    stale_test = f"-maxdepth 0 -mmin +{int(stale_minutes)} 2>/dev/null"
    # Newline-joined: `then; rm ...` is a bash syntax error.
    script = "\n".join(
        [
            f"mkdir -p {q_parent} 2>/dev/null || true",
            f"if err=$(mkdir {q_lock} 2>&1); then {claim}; exit 0; fi",
            f'if [ ! -d {q_lock} ]; then echo "$err" >&2; exit {_LOCK_UNCREATABLE}; fi',
            f'if [ -z "$(find {q_lock} {stale_test})" ]; then exit 1; fi',
            LOCK_SEAM_SEEN,
            # A breaker that died inside the break: its sub-lock goes stale like any lock.
            f'if [ -n "$(find {q_breaking} {stale_test})" ]; then rmdir {q_breaking} 2>/dev/null || true; fi',
            f"if ! mkdir {q_breaking} 2>/dev/null; then exit 1; fi",
            f"trap 'rmdir {q_breaking} 2>/dev/null || true' EXIT",
            # Under the sub-lock: the lock as it is now, not as it was when we looked.
            f"before=$(cat {q_owner_file} 2>/dev/null || echo '(no owner file)')",
            f'if [ -z "$(find {q_lock} {stale_test})" ]; then exit 1; fi',
            LOCK_SEAM_RECHECKED,
            f"stale={q_lock}.stale.$$.$RANDOM",
            f'if mv -T {q_lock} "$stale" 2>/dev/null; then',
            f"  {LOCK_SEAM_RENAMED}",
            f"  now=$(cat \"$stale/{LOCK_OWNER_FILE}\" 2>/dev/null || echo '(no owner file)')",
            f'  if [ "$now" != "$before" ] || [ -z "$(find "$stale" {stale_test})" ]; then',
            # Claimed or refreshed between our look and the rename: give it back, never over a new claim.
            f'    mv -T -n "$stale" {q_lock} 2>/dev/null || true',
            '    if [ -e "$stale" ]; then',
            f'      echo "lock: left $stale aside, not deleted: not the stale lock of {lock_dir} we looked at" >&2',
            "    fi",
            "    exit 1",
            "  fi",
            f'  echo "lock: broke the stale lock {lock_dir} (unchanged for over {int(stale_minutes)} min),'
            ' held by: $now"',
            '  rm -rf "$stale" 2>/dev/null || true',
            "fi",
            f"if mkdir {q_lock} 2>/dev/null; then {claim}; exit 0; fi",
            "exit 1",
        ]
    )
    proc = conn.execute(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if proc.returncode == _LOCK_UNCREATABLE:
        raise CIError(
            f"Cannot create the lock {lock_dir}: {_decode(stderr)}. No other run holds it; "
            "check the filesystem (quota, permissions) on the cluster."
        )
    for line in _decode(stdout).splitlines():
        if line.startswith("lock: broke"):
            print(f"::warning::{line}")
    for line in _decode(stderr).splitlines():
        if line.startswith("lock: left"):
            print(f"::warning::{line}")
    return int(proc.returncode) == 0


def lock_holder(conn: Connection, *, lock_dir: str) -> str:
    """The owner file of ``lock_dir`` (one line of JSON), or a note that there is none."""
    proc = conn.execute(
        ["bash", "-c", f"cat {shlex.quote(f'{lock_dir}/{LOCK_OWNER_FILE}')} 2>/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _stderr = proc.communicate()
    return _decode(stdout) or "(no owner file)"


def _release_lock(conn: Connection, *, lock_dir: str, owner: str, what: str) -> None:
    """Remove ``lock_dir`` only while its owner file is still ``owner``: a lock broken as stale and
    claimed by another run stays theirs. Always exits 0, so a propagating exception is not replaced."""
    q_lock = shlex.quote(lock_dir)
    q_owner_file = shlex.quote(f"{lock_dir}/{LOCK_OWNER_FILE}")
    script = "\n".join(
        [
            f"if {_owner_is(lock_dir, owner)}; then rm -rf {q_lock} 2>/dev/null || true",
            f"elif [ -d {q_lock} ]; then",
            f'  echo "::warning::not releasing the {what} lock {lock_dir}: it is no longer ours; held by:'
            f' $(cat {q_owner_file} 2>/dev/null || echo "(no owner file)")"',
            "fi",
            "exit 0",
        ]
    )
    proc = conn.execute(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, _stderr = proc.communicate()
    if out := _decode(stdout):
        print(out)


#: How often a holder with a heartbeat refreshes its lock; well under any stale threshold.
LOCK_HEARTBEAT_SECONDS: Final = 60


def _heartbeat(conn: Connection, *, lock_dir: str, owner: str, every: float, stop: threading.Event) -> None:
    """Refresh the lock's mtime while it is still ours, so a long hold never looks stale."""
    touch = f"if {_owner_is(lock_dir, owner)}; then touch {shlex.quote(lock_dir)}; fi"
    while not stop.wait(every):
        try:
            conn.execute(["bash", "-c", touch], stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            # A missed beat must not kill the holder; the next one retries.
            print(f"lock: heartbeat of {lock_dir} failed: {exc}")


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
    with remote_lock(
        conn,
        lock_dir=_ship_lock_path(staging_dir),
        run_id=run_id,
        what="staging",
        subject=staging_dir,
        timeout=timeout,
        poll=poll,
        stale_minutes=stale_minutes,
        dryrun=dryrun,
    ):
        yield


@contextmanager
def remote_lock(
    conn: Connection,
    *,
    lock_dir: str,
    run_id: str,
    what: str,
    subject: str,
    timeout: int = DEFAULT_SHIP_LOCK_TIMEOUT,
    poll: int = SHIP_LOCK_POLL_SECONDS,
    stale_minutes: int = SHIP_LOCK_STALE_MINUTES,
    dryrun: bool = False,
    heartbeat: float = 0,
) -> Iterator[None]:
    """Exclusive lock on the shared cluster filesystem; ``what`` and ``subject`` only label logs.

    ``mkdir`` claims it and an owner file (:func:`lock_owner`) says whose it is; a lock unchanged for
    ``stale_minutes`` is broken. With ``heartbeat`` (seconds) a thread refreshes it while held, so
    only a dead holder's lock goes stale. Released only while the owner file is still ours.
    """
    if dryrun:
        yield
        return
    owner = lock_owner(run_id)
    deadline = time.monotonic() + timeout
    waited = False
    while not _try_acquire_lock(conn, lock_dir=lock_dir, run_id=run_id, stale_minutes=stale_minutes, owner=owner):
        if time.monotonic() >= deadline:
            raise CIError(
                f"Timed out after {timeout}s waiting for the {what} lock {lock_dir}, held by: "
                f"{lock_holder(conn, lock_dir=lock_dir)}. Remove the lock directory if that run is gone; "
                f"it is broken by itself once unchanged for {stale_minutes} min."
            )
        if not waited:
            waited = True
            print(
                f"{what}: '{subject}' is locked by another run; waiting up to {timeout}s. "
                f"Held by: {lock_holder(conn, lock_dir=lock_dir)}"
            )
        time.sleep(poll)
    stop = threading.Event()
    beat = None
    if heartbeat > 0:
        beat = threading.Thread(
            target=_heartbeat,
            kwargs={"conn": conn, "lock_dir": lock_dir, "owner": owner, "every": heartbeat, "stop": stop},
            daemon=True,
        )
        beat.start()
    try:
        yield
    finally:
        stop.set()
        if beat is not None:
            beat.join(timeout=30)
        _release_lock(conn, lock_dir=lock_dir, owner=owner, what=what)


def _tree_stats(path: str | Path) -> tuple[int, int]:
    """(files, bytes) under ``path``, for the transfer timings."""
    files = size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                size += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return files, size


def _mib(size: int) -> str:
    return f"{size / 2**20:.1f} MiB"


def _reset_staging_dir(conn: Connection, *, staging_dir: str, run_id: str) -> None:
    """Rename-aside, avoiding ``ENOTEMPTY`` races; call only under :func:`ship_lock`."""
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
    """Reset ``staging_dir``, ship the checkout and dep prefixes (to ``<remote_deps_dir>/<i>``), then the marker.

    The marker comes last so the job never sees a partial copy.
    """
    if dryrun:
        return
    remote_tgz = str(PurePosixPath(staging_dir) / jobscript.SOURCE_TARBALL_NAME)
    local_tgz = Path(tar_dir) / f"{run_id}.src.tgz"

    start = time.monotonic()
    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    files, size = _tree_stats(local_source_dir)
    subprocess.run(["tar", "-czf", str(local_tgz), "-C", str(local_source_dir), "."], check=True)
    tarred = time.monotonic()
    _reset_staging_dir(conn, staging_dir=staging_dir, run_id=run_id)
    reset = time.monotonic()
    conn.sendfile(local_tgz, remote_tgz)
    sent = time.monotonic()
    print(
        f"ship: source ({files} files, {_mib(size)}, {_mib(local_tgz.stat().st_size)} gzipped): "
        f"tar {tarred - start:.0f}s, staging reset {reset - tarred:.0f}s, send {sent - reset:.0f}s"
    )
    local_tgz.unlink()
    if remote_deps_dir is not None:
        for index, (prefix, name) in enumerate(zip(local_prefixes, remote_dep_names(local_prefixes), strict=True)):
            push_tree(
                conn,
                local_dir=prefix,
                remote_dir=f"{remote_deps_dir.rstrip('/')}/{name}",
                tar_dir=tar_dir,
                tarball_suffix="",
                local_tar_name=f"{run_id}.dep{index}.tgz",
            )
    _run_remote(conn, ["touch", _marker_path(staging_dir)], what="Transfer-complete marker")
    print(f"ship: done in {time.monotonic() - start:.0f}s")


def fetch_tree(
    conn: Connection,
    *,
    remote_dir: str,
    local_dir: str,
    tar_dir: str,
    tarball_suffix: str = "fetch",
    dryrun: bool = False,
) -> None:
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
    local_tgz.unlink()


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
    """An empty ``tarball_suffix`` gives ``<dir>.tgz``."""
    if dryrun:
        return
    name = PurePosixPath(remote_dir).name
    ext = f".{tarball_suffix}.tgz" if tarball_suffix else ".tgz"
    local_tgz = Path(tar_dir) / (local_tar_name or f"{name}{ext}")
    remote_tgz = f"{remote_dir.rstrip('/')}{ext}"

    start = time.monotonic()
    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    files, size = _tree_stats(local_dir)
    subprocess.run(["tar", "-czf", str(local_tgz), "-C", str(local_dir), "."], check=True)
    tarred = time.monotonic()
    _run_remote(conn, ["mkdir", "-p", remote_dir], what=f"Remote mkdir of {remote_dir}")
    conn.sendfile(local_tgz, remote_tgz)
    sent = time.monotonic()
    gzipped = local_tgz.stat().st_size
    local_tgz.unlink()
    _run_remote(
        conn,
        ["bash", "-c", f"tar -xzf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_dir)}"],
        what="Remote tree unpack",
    )
    print(
        f"ship: {Path(local_dir).name} ({files} files, {_mib(size)}, {_mib(gzipped)} gzipped): "
        f"tar {tarred - start:.0f}s, send {sent - tarred:.0f}s, unpack {time.monotonic() - sent:.0f}s"
    )


def remove_tree(conn: Connection, *, remote_dir: str, dryrun: bool = False) -> None:
    """Also removes leftover transfer tarballs; the caller guards top-level paths."""
    if dryrun:
        return
    base = remote_dir.rstrip("/")
    _run_remote(
        conn,
        ["rm", "-rf", base, f"{base}.push.tgz", f"{base}.fetch.tgz"],
        what=f"Remote remove of {remote_dir}",
    )


def _unzstd_into(archive: Path, dest: Path) -> None:
    """Via the zstd binary: portable across GNU tar and bsdtar."""
    dec = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
    try:
        untar = subprocess.run(["tar", "-xf", "-", "-C", str(dest)], stdin=dec.stdout)
    finally:
        if dec.stdout is not None:
            dec.stdout.close()
        decode_rc = dec.wait()
    if decode_rc != 0 or untar.returncode != 0:
        raise CIError(f"Unpacking {archive} failed (zstd exit {decode_rc}, tar exit {untar.returncode})")


def fetch_install(
    conn: Connection,
    *,
    remote_install_dir: str,
    local_install_dir: str,
    tar_dir: str,
    dryrun: bool = False,
) -> None:
    """Fetch and unpack the archive the job wrote at ``CI_INSTALL_ARCHIVE``."""
    if dryrun:
        return
    remote_tgz = jobscript.install_archive_path(remote_install_dir)
    local_tgz = Path(tar_dir) / PurePosixPath(remote_tgz).name

    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    conn.getfile(remote_tgz, local_tgz)
    Path(local_install_dir).mkdir(parents=True, exist_ok=True)
    _unzstd_into(local_tgz, Path(local_install_dir))
    local_tgz.unlink()
