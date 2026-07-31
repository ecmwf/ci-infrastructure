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
    tarball into the shared staging dir and finally ``touch``es a
    ``TRANSFER_COMPLETED_<run_id>`` marker; the already-submitted job blocks on
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
from collections.abc import Sequence
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


def touch_remote_file(conn: Connection, *, path: str) -> None:
    """Create ``path`` (and its parent) on the remote if it does not exist."""
    parent = str(PurePosixPath(path).parent)
    _run_remote(conn, ["mkdir", "-p", parent], what=f"Remote mkdir of {parent}")
    _run_remote(conn, ["touch", path], what=f"Remote touch of {path}")


def _marker_path(staging_dir: str, run_id: str) -> str:
    return str(PurePosixPath(staging_dir) / f"TRANSFER_COMPLETED_{run_id}")


def marker_exists(conn: Connection, *, staging_dir: str, run_id: str) -> bool:
    """Whether the source-transfer marker for ``run_id`` is present in the staging dir.

    Used on reattach: if a job was submitted but the runner died before the scp
    or the marker, the still-waiting job needs the source (re-)shipped.
    """
    proc = conn.execute(
        ["test", "-f", _marker_path(staging_dir, run_id)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    proc.communicate()
    return bool(proc.returncode == 0)


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
    so cleanup never fails the ship. A peer that renamed it first leaves the
    ``mv`` a harmless no-op; the concurrent shippers then write byte-identical
    inputs (same source SHA, same resolved deps) into the fresh tree.

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
            _ship_prefix(
                conn,
                local_prefix=prefix,
                remote_dir=f"{remote_deps_dir.rstrip('/')}/{index}",
                tar_path=Path(tar_dir) / f"{run_id}.dep{index}.tgz",
            )
    # Marker last: the job must never see it before every input is fully staged.
    _run_remote(conn, ["touch", _marker_path(staging_dir, run_id)], what="Transfer-complete marker")


def _ship_prefix(conn: Connection, *, local_prefix: str, remote_dir: str, tar_path: Path) -> None:
    """Tar a runner-local dependency prefix, scp it over and unpack it into ``remote_dir``."""
    subprocess.run(["tar", "-czf", str(tar_path), "-C", str(local_prefix), "."], check=True)
    remote_tgz = f"{remote_dir}.tgz"
    _run_remote(conn, ["mkdir", "-p", remote_dir], what=f"Remote dep dir creation ({remote_dir})")
    conn.sendfile(tar_path, remote_tgz)
    _run_remote(
        conn,
        ["bash", "-c", f"tar -xzf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_dir)}"],
        what=f"Remote dep unpack into {remote_dir}",
    )


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
    dryrun: bool = False,
) -> None:
    """Tar ``local_dir`` on the runner and unpack it into ``remote_dir`` on the cluster.

    The generic runner->HPC mirror of :func:`fetch_tree`: local tar -> ``sendfile``
    -> remote ``mkdir -p`` + untar. Like ``fetch_tree`` it needs no scheduler, so
    it works against ``direct`` sites too. (This is the same shape as the private
    ``_ship_prefix``, which stays tied to the source-shipping flow.)
    """
    if dryrun:
        return
    name = PurePosixPath(remote_dir).name
    local_tgz = Path(tar_dir) / f"{name}.{tarball_suffix}.tgz"
    remote_tgz = f"{remote_dir.rstrip('/')}.{tarball_suffix}.tgz"

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
