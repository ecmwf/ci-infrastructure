# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Promote an HPC release build into its prefix: unpack the tested archive there, once.

Every step is idempotent, so "Re-run failed jobs" redoes what an interrupted promote left:

1. On the runner: the archive is the bytes the in-job install test passed (``release_tree``), and
   safe to unpack (``release.archive``).
2. ``<lock-root>/DISABLED`` on the cluster stops every promote (the HPC side of the kill switch).
3. Under the lock ``<lock-root>/promote/<package>`` (owner, heartbeat, stale recovery: ``transfer.remote_lock``):
   - the prefix holds ``.release.json`` of this release with the same digest: nothing to do;
     with another digest, or another release: refused, never overwritten;
   - else: leftovers of an interrupted promote (``<parent>/.<leaf>.incoming.*``) are removed, the
     archive is sent to ``<lock-root>/staging`` and its digest checked there, unpacked into a new
     ``.incoming`` sibling with ``.release.json`` and ``build.log``, made read-only, and renamed
     onto the prefix (``mv -T -n``: atomic, never over an existing tree), then read back.
4. A receipt (``receipt-<leg_id>.json``) says what was done.

A build skipped as already published (``state.json``) is only checked against the prefix.
Milestone 1 installs under a dry-run root; ``/usr/local/apps`` is refused.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .._errors import CIError
from .._github_api import write_outputs
from ..release import ReleaseError
from ..release.archive import check_archive
from ..release.legs import check_install_prefix
from . import release_tree, transfer
from .release_tree import LocalBuild
from .site import resolve_remote_path

DEFAULT_LOCK_ROOT: Final = "$HOME/.cd-locks"
DEFAULT_LOCK_TIMEOUT: Final = 1800
DEFAULT_LOCK_STALE_MINUTES: Final = 120
#: A file of this name in the lock root stops every promote and sync/tag on this HPC.
KILL_SWITCH_FILE: Final = "DISABLED"
INCOMING: Final = ".incoming."

_LOCK_ROOT_RE: Final = re.compile(r"^(?:\$(?:HOME|SCRATCH|PERM|HPCPERM)(?:/[A-Za-z0-9._-]+)*|(?:/[A-Za-z0-9._-]+)+)$")
_RUN_ID_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")


def check_lock_root(raw: str) -> str:
    """``$HOME/...`` (``~/...`` too), ``$SCRATCH/...``, ``$PERM/...``, ``$HPCPERM/...`` or absolute."""
    root = raw.strip() or DEFAULT_LOCK_ROOT
    if root == "~" or root.startswith("~/"):
        root = "$HOME" + root[1:]
    root = root.rstrip("/")
    parts = root.split("/")[1:]
    if not _LOCK_ROOT_RE.fullmatch(root) or any(c in (".", "..") for c in parts):
        raise CIError(
            f"lock root {raw!r} must be $HOME/<dir>... ($SCRATCH, $PERM, $HPCPERM too) or an absolute path, "
            "each component of [A-Za-z0-9._-]"
        )
    return root


def run_id_from_env(env: Mapping[str, str] | None = None) -> str:
    """``<run id>-<attempt>``: names the incoming dir, so two attempts never share one."""
    e = os.environ if env is None else env
    run_id = f"{e.get('GITHUB_RUN_ID', '')}-{e.get('GITHUB_RUN_ATTEMPT', '')}".strip("-") or f"local-{os.getpid()}"
    if not _RUN_ID_RE.fullmatch(run_id):
        raise CIError(f"run id {run_id!r} is not a safe path component")
    return run_id


def _run(conn: Any, script: str, what: str) -> str:
    proc = conn.execute(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    out = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr or "").strip()
        raise CIError(f"{what} failed (exit {proc.returncode}): {err or out.strip()}")
    return out


def check_kill_switch(conn: Any, lock_root: str) -> None:
    """Raise unless the cluster says, with exit 0, that ``<lock_root>/DISABLED`` is absent.

    Exit 3 is the switch; any other failure (ssh 255, no bash, a filesystem error) is not proof
    that CD is enabled, so it stops the job too.
    """
    switch = f"{lock_root}/{KILL_SWITCH_FILE}"
    q = shlex.quote(switch)
    proc = conn.execute(
        ["bash", "-c", f"if [ -e {q} ] || [ -L {q} ]; then cat {q}; exit 3; fi\nexit 0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate()
    out = stdout.decode(errors="replace").strip() if isinstance(stdout, bytes) else str(stdout or "").strip()
    if proc.returncode == 0:
        return
    if proc.returncode == 3:
        raise CIError(f"CD is disabled on this HPC: {switch} exists ({out or 'no reason given'}); remove it to resume")
    err = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr or "").strip()
    raise CIError(
        f"Cannot tell whether CD is disabled on this HPC: checking for {switch} failed "
        f"(exit {proc.returncode}): {err or out or 'no output'}; nothing was changed"
    )


def receipt(ctx: Mapping[str, Any], *, prefix: str, sha256: str, action: str) -> dict[str, Any]:
    return {
        "url": prefix,
        "prefix": prefix,
        "filename": ctx.get("filename"),
        "sha256": sha256,
        "action": action,
        "leg_id": ctx.get("leg_id"),
        "job_key": ctx.get("job_key"),
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def release_json(ctx: Mapping[str, Any], *, prefix: str, sha256: str, run: Mapping[str, str]) -> dict[str, Any]:
    """What ``<prefix>/.release.json`` says: this release, its bytes, and the run that put them there."""
    return {
        **release_tree.identity(ctx),
        "sha256": sha256,
        "mode": ctx.get("mode"),
        "version": ctx.get("version"),
        "pkg_version": ctx.get("pkg_version"),
        "head_sha": ctx.get("head_sha"),
        "filename": ctx.get("filename"),
        "prefix": prefix,
        "prefix_spec": ctx.get("prefix"),
        "promoted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": dict(run),
    }


def _settle(existing: Mapping[str, Any], ctx: Mapping[str, Any], build: LocalBuild, prefix: str) -> dict[str, Any]:
    """The prefix holds a release already: the same one with the same bytes is done; anything else is refused."""
    if not release_tree.same_release(existing, ctx):
        raise CIError(
            f"{prefix} holds another release ({release_tree.describe(existing)}), not this one "
            f"({release_tree.describe(ctx)}): versions are immutable; tag a new rc"
        )
    installed = str(existing.get("sha256") or "")
    if build.state == release_tree.STATE_ALREADY_PUBLISHED:
        probed = str((build.installed or {}).get("sha256") or "")
        if probed and probed != installed:
            raise CIError(f"{prefix} changed since the build job looked (sha256 {probed}, now {installed}): refusing")
        print(f"{prefix} holds this release (sha256 {installed}); its build was skipped: nothing to do")
        return receipt(ctx, prefix=prefix, sha256=installed, action="already-published")
    if installed != build.sha256:
        raise CIError(
            f"{prefix} holds this release from other bytes (sha256 {installed}, this build {build.sha256}): "
            "an installed tree is never replaced; tag a new rc"
        )
    print(f"{prefix} holds these bytes already (sha256 {installed}): nothing to do")
    return receipt(ctx, prefix=prefix, sha256=installed, action="already-present")


def promote(
    conn: Any,
    *,
    ctx: Mapping[str, Any],
    build: LocalBuild,
    prefix: str,
    lock_root: str,
    run_id: str,
    run: Mapping[str, str],
    timeout: int = DEFAULT_LOCK_TIMEOUT,
    stale_minutes: int = DEFAULT_LOCK_STALE_MINUTES,
    poll: int = transfer.SHIP_LOCK_POLL_SECONDS,
    heartbeat: float = transfer.LOCK_HEARTBEAT_SECONDS,
) -> dict[str, Any]:
    """Promote ``build`` to ``prefix`` (both resolved on the cluster); the receipt."""
    check_kill_switch(conn, lock_root)
    package = str(ctx["package_name"])
    with transfer.remote_lock(
        conn,
        lock_dir=f"{lock_root}/promote/{package}",
        run_id=run_id,
        what="promote",
        subject=f"{package} {ctx.get('pkg_version')}",
        timeout=timeout,
        poll=poll,
        stale_minutes=stale_minutes,
        heartbeat=heartbeat,
    ):
        # Again under the lock: the switch may have been set while we waited.
        check_kill_switch(conn, lock_root)
        existing = release_tree.read_release_json(conn, prefix)
        if existing is not None:
            return _settle(existing, ctx, build, prefix)
        if build.archive is None:
            raise CIError(
                f"the build was skipped as {prefix} held this release, but {prefix} is empty now: re-run all jobs"
            )
        return _install(conn, ctx=ctx, build=build, prefix=prefix, lock_root=lock_root, run_id=run_id, run=run)


def _install(
    conn: Any,
    *,
    ctx: Mapping[str, Any],
    build: LocalBuild,
    prefix: str,
    lock_root: str,
    run_id: str,
    run: Mapping[str, str],
) -> dict[str, Any]:
    assert build.archive is not None
    parent, leaf = str(PurePosixPath(prefix).parent), PurePosixPath(prefix).name
    incoming = f"{parent}/.{leaf}{INCOMING}{run_id}"
    staged = f"{lock_root}/staging/{ctx['job_key']}.tar.zst"
    q_parent, q_incoming, q_staged, q_prefix = (shlex.quote(p) for p in (parent, incoming, staged, prefix))
    pattern = f"{shlex.quote(f'{parent}/.{leaf}{INCOMING}')}*"
    # Leftovers of an interrupted promote of this prefix: read-only trees, so writable first.
    print(
        _run(
            conn,
            "\n".join(
                [
                    f"mkdir -p {q_parent} {shlex.quote(f'{lock_root}/staging')}",
                    f"for d in {pattern}; do",
                    '  [ -e "$d" ] || continue',
                    '  echo "promote: removing what an interrupted promote left: $d"',
                    '  chmod -R u+w "$d" && rm -rf "$d"',
                    "done",
                ]
            ),
            "Cleaning up earlier promotes",
        ),
        end="",
    )
    conn.sendfile(build.archive, staged)
    there = _run(conn, f"sha256sum {q_staged} | cut -d' ' -f1", "Checking the sent archive").strip()
    if there != build.sha256:
        raise CIError(f"the archive arrived as sha256 {there}, sent {build.sha256}")
    _run(
        conn,
        f"mkdir {q_incoming} && zstd -dc {q_staged} | tar -x --no-same-owner -C {q_incoming}",
        f"Unpacking into {incoming}",
    )
    record = release_json(ctx, prefix=prefix, sha256=build.sha256, run=run)
    sidecars: list[tuple[Path, str]] = []
    with tempfile.TemporaryDirectory(prefix="ci-promote-") as tmp:
        local = Path(tmp) / release_tree.RELEASE_JSON
        local.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        sidecars.append((local, release_tree.RELEASE_JSON))
        if build.build_log is not None:
            sidecars.append((build.build_log, release_tree.BUILD_LOG))
        for src, name in sidecars:
            _place_sidecar(conn, src, staged=f"{lock_root}/staging/{ctx['job_key']}.{name}", dest=f"{incoming}/{name}")
    _run(
        conn,
        "\n".join(
            [
                f"chmod -R a-w {q_incoming}",
                # Same parent: an atomic rename, which -n and -T keep from ever landing on or in an existing tree.
                f"mv -T -n {q_incoming} {q_prefix} || true",
                f'if [ -e {q_incoming} ]; then echo "{prefix} appeared meanwhile; left {incoming}" >&2; exit 1; fi',
                f"rm -f {q_staged}",
            ]
        ),
        f"Moving {incoming} onto {prefix}",
    )
    installed = release_tree.read_release_json(conn, prefix)
    if installed is None or installed.get("sha256") != build.sha256 or not release_tree.same_release(installed, ctx):
        raise CIError(f"{prefix}/{release_tree.RELEASE_JSON} does not read back as this release: {installed}")
    print(f"promoted {ctx.get('filename')} (sha256 {build.sha256}) to {prefix}")
    return receipt(ctx, prefix=prefix, sha256=build.sha256, action="promoted")


def _place_sidecar(conn: Any, src: Path, *, staged: str, dest: str) -> None:
    """Send ``src`` to ``staged`` (ours, under the lock root), then move it to ``dest`` only while
    nothing (not even a dangling symlink) is there: never written through a link the archive made."""
    q_staged, q_dest = shlex.quote(staged), shlex.quote(dest)
    _run(conn, f"rm -f {q_staged}", f"Clearing {staged}")
    conn.sendfile(src, staged)
    _run(
        conn,
        "\n".join(
            [
                f"if [ -e {q_dest} ] || [ -L {q_dest} ]; then",
                f'  echo "{dest} exists already: the archive carries it; refusing to write through it" >&2',
                f"  rm -f {q_staged}; exit 1",
                "fi",
                f"mv -T -n {q_staged} {q_dest}",
                f'if [ -e {q_staged} ]; then rm -f {q_staged}; echo "{dest} appeared meanwhile" >&2; exit 1; fi',
            ]
        ),
        f"Writing {dest}",
    )


def run_promote(
    conn: Any,
    *,
    matrix_leg: str,
    archive_dir: Path,
    lock_root: str,
    receipt_dir: Path,
    timeout: int = DEFAULT_LOCK_TIMEOUT,
    stale_minutes: int = DEFAULT_LOCK_STALE_MINUTES,
    poll: int = transfer.SHIP_LOCK_POLL_SECONDS,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The CLI's work: check the build on the runner, resolve the paths on the cluster, promote, write the receipt."""
    e = os.environ if env is None else env
    ctx = release_tree.release_context(matrix_leg)
    if ctx.get("mode") == "rehearsal":
        raise CIError("a rehearsal is never promoted")
    build = release_tree.check_local_build(archive_dir, ctx)
    if build.archive is not None:
        try:
            members = check_archive(build.archive, reserved=(release_tree.RELEASE_JSON, release_tree.BUILD_LOG))
        except ReleaseError as exc:
            raise CIError(str(exc)) from exc
        print(f"{build.archive.name}: {members} members, safe to unpack; sha256 {build.sha256}, install-tested")
    prefix = resolve_remote_path(conn, str(ctx["prefix"]))
    root = resolve_remote_path(conn, check_lock_root(lock_root))
    try:
        check_install_prefix(prefix)
    except ReleaseError as exc:
        raise CIError(f"the release prefix {ctx['prefix']} is {prefix} on the cluster: {exc}") from exc
    server, repo = e.get("GITHUB_SERVER_URL", ""), e.get("GITHUB_REPOSITORY", "")
    run = {
        "repository": repo,
        "run_url": f"{server}/{repo}/actions/runs/{e.get('GITHUB_RUN_ID', '')}" if server and repo else "",
        "attempt": e.get("GITHUB_RUN_ATTEMPT", ""),
    }
    result = promote(
        conn,
        ctx=ctx,
        build=build,
        prefix=prefix,
        lock_root=root,
        run_id=run_id_from_env(e),
        run=run,
        timeout=timeout,
        stale_minutes=stale_minutes,
        poll=poll,
    )
    receipt_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_dir / f"receipt-{ctx['leg_id']}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    write_outputs({"receipt": str(path)})
    return result
