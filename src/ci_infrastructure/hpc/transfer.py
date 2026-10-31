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
            # Private: it may hold the sccache credentials.
            f"mkdir -p {q_staging}",
            f"chmod 700 {q_staging}",
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
    extra_files: Sequence[tuple[str, str, int]] = (),
    dryrun: bool = False,
) -> None:
    """Reset ``staging_dir``, ship the checkout, dep prefixes (to ``<remote_deps_dir>/<i>``) and ``extra_files``
    (local path, path under staging, mode), then the marker, so the job sees no partial copy.
    """
    if dryrun:
        return
    remote_archive = str(PurePosixPath(staging_dir) / jobscript.SOURCE_TARBALL_NAME)
    local_archive = Path(tar_dir) / f"{run_id}.src.tar.zst"

    start = time.monotonic()
    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    files, size = _tree_stats(local_source_dir)
    _zstd_tar(Path(local_source_dir), local_archive)
    tarred = time.monotonic()
    _reset_staging_dir(conn, staging_dir=staging_dir, run_id=run_id)
    reset = time.monotonic()
    conn.sendfile(local_archive, remote_archive)
    sent = time.monotonic()
    print(
        f"ship: source ({files} files, {_mib(size)}, {_mib(local_archive.stat().st_size)} compressed): "
        f"tar {tarred - start:.0f}s, staging reset {reset - tarred:.0f}s, send {sent - reset:.0f}s"
    )
    local_archive.unlink()
    if remote_deps_dir is not None:
        for index, (prefix, name) in enumerate(zip(local_prefixes, remote_dep_names(local_prefixes), strict=True)):
            push_tree(
                conn,
                local_dir=prefix,
                remote_dir=f"{remote_deps_dir.rstrip('/')}/{name}",
                tar_dir=tar_dir,
                tarball_suffix="",
                local_tar_name=f"{run_id}.dep{index}.tar.zst",
            )
    for local, rel, mode in extra_files:
        remote = str(PurePosixPath(staging_dir) / rel)
        _run_remote(conn, ["mkdir", "-p", str(PurePosixPath(remote).parent)], what=f"Remote mkdir for {rel}")
        conn.sendfile(local, remote)
        _run_remote(conn, ["chmod", f"{mode:o}", remote], what=f"Remote chmod of {rel}")
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
    remote_archive = str(remote.parent / f"{remote.name}.{tarball_suffix}.tar.zst")
    local_archive = Path(tar_dir) / f"{remote.name}.{tarball_suffix}.tar.zst"

    _run_remote(
        conn,
        [
            "bash",
            "-c",
            f"set -o pipefail; tar -cf - -C {shlex.quote(remote_dir)} . "
            f"| zstd -T0 -1 -q -f -o {shlex.quote(remote_archive)}",
        ],
        what="Remote tree tar",
    )
    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    conn.getfile(remote_archive, local_archive)
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    _unzstd_into(local_archive, Path(local_dir))
    local_archive.unlink()


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
    """An empty ``tarball_suffix`` gives ``<dir>.tar.zst``."""
    if dryrun:
        return
    name = PurePosixPath(remote_dir).name
    ext = f".{tarball_suffix}.tar.zst" if tarball_suffix else ".tar.zst"
    local_archive = Path(tar_dir) / (local_tar_name or f"{name}{ext}")
    remote_archive = f"{remote_dir.rstrip('/')}{ext}"

    start = time.monotonic()
    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    files, size = _tree_stats(local_dir)
    _zstd_tar(Path(local_dir), local_archive)
    tarred = time.monotonic()
    _run_remote(conn, ["mkdir", "-p", remote_dir], what=f"Remote mkdir of {remote_dir}")
    conn.sendfile(local_archive, remote_archive)
    sent = time.monotonic()
    compressed = local_archive.stat().st_size
    local_archive.unlink()
    _run_remote(
        conn,
        [
            "bash",
            "-c",
            f"set -o pipefail; zstd -dc {shlex.quote(remote_archive)} | tar -xf - -C {shlex.quote(remote_dir)}",
        ],
        what="Remote tree unpack",
    )
    print(
        f"ship: {Path(local_dir).name} ({files} files, {_mib(size)}, {_mib(compressed)} compressed): "
        f"tar {tarred - start:.0f}s, send {sent - tarred:.0f}s, unpack {time.monotonic() - sent:.0f}s"
    )


def remove_tree(conn: Connection, *, remote_dir: str, dryrun: bool = False) -> None:
    """Also removes leftover transfer tarballs; the caller guards top-level paths."""
    if dryrun:
        return
    base = remote_dir.rstrip("/")
    _run_remote(
        conn,
        ["rm", "-rf", base, f"{base}.push.tar.zst", f"{base}.fetch.tar.zst"],
        what=f"Remote remove of {remote_dir}",
    )


def _zstd_tar(src: Path, archive: Path) -> None:
    """Multithreaded at the fastest level: a single-threaded gzip took minutes on large checkouts."""
    tar = subprocess.Popen(["tar", "-cf", "-", "-C", str(src), "."], stdout=subprocess.PIPE)
    try:
        enc = subprocess.run(["zstd", "-T0", "-1", "-q", "-f", "-o", str(archive)], stdin=tar.stdout)
    finally:
        if tar.stdout is not None:
            tar.stdout.close()
        tar_rc = tar.wait()
    if tar_rc != 0 or enc.returncode != 0:
        raise CIError(f"Packing {src} failed (tar exit {tar_rc}, zstd exit {enc.returncode})")


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
    remote_archive = jobscript.install_archive_path(remote_install_dir)
    local_archive = Path(tar_dir) / PurePosixPath(remote_archive).name

    Path(tar_dir).mkdir(parents=True, exist_ok=True)
    conn.getfile(remote_archive, local_archive)
    Path(local_install_dir).mkdir(parents=True, exist_ok=True)
    _unzstd_into(local_archive, Path(local_install_dir))
    local_archive.unlink()
