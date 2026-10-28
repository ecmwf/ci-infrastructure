# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Move trees between runner and cluster over troika's connection, one tarball each (it sends one file at a time)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from .._errors import CIError
from . import jobscript


class Connection(Protocol):
    def execute(self, command: Any, stdout: Any = ..., stderr: Any = ..., dryrun: bool = ...) -> Any: ...

    def sendfile(self, src: Any, dst: Any, dryrun: bool = ...) -> None: ...

    def getfile(self, src: Any, dst: Any, dryrun: bool = ...) -> None: ...


def output_text(out: Any) -> str:
    """A troika process's stdout or stderr as text: bytes, or a str in tests."""
    return out.decode(errors="replace") if isinstance(out, bytes) else str(out)


def _run_remote(conn: Connection, argv: list[str], *, what: str, dryrun: bool = False) -> None:
    proc = conn.execute(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, dryrun=dryrun)
    if dryrun:
        return
    _stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        detail = output_text(stderr).strip()
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
    """Each prefix's own directory name, so a relative RPATH resolves as on the runner; else indices."""
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


def _try_acquire_lock(conn: Connection, *, lock_dir: str, run_id: str, stale_minutes: int) -> bool:
    """One atomic ``mkdir`` (no ``-p``) claim, breaking a lock older than ``stale_minutes``."""
    q_lock = shlex.quote(lock_dir)
    q_owner = shlex.quote(f"{lock_dir}/owner")
    q_parent = shlex.quote(str(PurePosixPath(lock_dir).parent))
    owner = f"{{ echo {shlex.quote(run_id)} > {q_owner} 2>/dev/null || true; }}"
    # Newline-joined: `then; rm ...` is a bash syntax error.
    script = "\n".join(
        [
            f"mkdir -p {q_parent} 2>/dev/null || true",
            f"if err=$(mkdir {q_lock} 2>&1); then {owner}; exit 0; fi",
            f'if [ ! -d {q_lock} ]; then echo "$err" >&2; exit {_LOCK_UNCREATABLE}; fi',
            f'if [ -n "$(find {q_lock} -maxdepth 0 -mmin +{stale_minutes} 2>/dev/null)" ]; then',
            f"  rm -rf {q_lock} 2>/dev/null || true",
            f"  if mkdir {q_lock} 2>/dev/null; then {owner}; exit 0; fi",
            "fi",
            "exit 1",
        ]
    )
    proc = conn.execute(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _stdout, stderr = proc.communicate()
    if proc.returncode == _LOCK_UNCREATABLE:
        detail = output_text(stderr).strip()
        raise CIError(
            f"Cannot create the lock {lock_dir}: {detail}. No other run holds it; "
            "check the filesystem (quota, permissions) on the cluster."
        )
    return int(proc.returncode) == 0


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
) -> Iterator[None]:
    """Exclusive lock on the shared cluster filesystem; ``what`` and ``subject`` only label logs."""
    if dryrun:
        yield
        return
    deadline = time.monotonic() + timeout
    waited = False
    while not _try_acquire_lock(conn, lock_dir=lock_dir, run_id=run_id, stale_minutes=stale_minutes):
        if time.monotonic() >= deadline:
            raise CIError(
                f"Timed out after {timeout}s waiting for the {what} lock {lock_dir}. "
                "Another run holds it for this artifact; remove the lock directory if its runner is gone."
            )
        if not waited:
            waited = True
            print(f"{what}: '{subject}' is locked by another run; waiting up to {timeout}s.")
        time.sleep(poll)
    try:
        yield
    finally:
        # Always exit 0: a failed release must not replace a propagating exception.
        _run_remote(
            conn,
            ["bash", "-c", f"rm -rf {shlex.quote(lock_dir)} 2>/dev/null || true"],
            what=f"Release of {what} lock {lock_dir}",
        )


def _tree_stats(path: str | Path) -> tuple[int, int]:
    """(files, bytes) under ``path``."""
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
    """Reset ``staging_dir``, ship the checkout and dep prefixes, then the marker, so the job sees no partial copy."""
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
