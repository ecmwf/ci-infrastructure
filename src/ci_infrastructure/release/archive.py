# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Is a release archive safe to unpack into an install prefix?

An HPC module is promoted by unpacking the build's ``.tar.zst`` under its prefix, as the deploy
identity. The archive came from a job that ran the repo's own code, so before any byte of it
lands there it is read (never extracted) here and refused when a member could write outside the
tree or carries what an install tree never needs: an absolute or ``..`` name, a symlink or hard
link that leads out of the tree, a device or FIFO, a setuid or setgid bit, or a name twice.

A symlink's target is resolved through the archive's own symlinks, as the kernel would once the
tree is unpacked (``a -> .``, ``b -> a/..`` leaves the tree although ``b``'s text does not say
so), and refused when any step leaves the tree, is absolute, or loops.
"""

from __future__ import annotations

import posixpath
import subprocess
import tarfile
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Final

from . import ReleaseError


def _normalised(name: str) -> str:
    """``./lib/x``, ``lib//x``, ``lib/./x`` -> ``lib/x`` (the spelling the file system ends up with);
    the archive root (``.``) -> ``""``. ``..`` is checked on the raw name before this is used."""
    name = posixpath.normpath(name) if name else "."
    return "" if name == "." else name


def _inside(path: str) -> bool:
    """``path`` (relative, posix) stays inside the tree once ``..`` is resolved lexically."""
    if path.startswith("/"):
        return False
    return posixpath.normpath(path or ".").split("/")[0] != ".."


#: How many symlinks one path may pass through before it counts as a loop (the kernel allows 40).
MAX_SYMLINK_HOPS: Final = 40


class _Outside(Exception):
    pass


def _resolve(path: str, links: Mapping[str, str]) -> str:
    """``path`` (relative to the root) with every symlink of ``links`` on it followed; raise
    :class:`_Outside` when a step leaves the tree, a target is absolute, or it loops."""
    todo = [c for c in path.split("/") if c not in ("", ".")]
    done: list[str] = []
    hops = 0
    while todo:
        part = todo.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not done:
                raise _Outside("leaves the tree")
            done.pop()
            continue
        here = "/".join([*done, part])
        if here in links:
            hops += 1
            if hops > MAX_SYMLINK_HOPS:
                raise _Outside("a symlink loop")
            target = links[here]
            if target.startswith("/"):
                raise _Outside(f"passes through {here!r} -> {target!r}, absolute")
            todo = target.split("/") + todo
            continue
        done.append(part)
    return "/".join(done)


def check_tar(path: Path, *, reserved: Collection[str] = ()) -> int:
    """Raise :class:`ReleaseError` naming every unsafe member of the plain tar at ``path``; the member count.

    ``reserved``: names at the archive root that the caller writes itself, refused as any member type.
    """
    problems: list[str] = []
    seen: set[str] = set()
    links: dict[str, str] = {}
    count = 0
    try:
        with tarfile.open(path, "r:") as tar:
            for member in tar:
                count += 1
                name = _normalised(member.name)
                where = repr(member.name)
                if name in reserved:
                    problems.append(f"{where}: a reserved name, written beside the tree by the release itself")
                if member.name.startswith("/"):
                    problems.append(f"{where}: absolute name")
                    continue
                if ".." in member.name.split("/"):
                    problems.append(f"{where}: a '..' component")
                    continue
                if name in seen and not member.isdir():
                    problems.append(f"{where}: in the archive twice")
                seen.add(name)
                if member.ischr() or member.isblk() or member.isfifo():
                    problems.append(f"{where}: a device or FIFO")
                if member.mode & 0o6000:
                    problems.append(f"{where}: setuid or setgid (mode {member.mode:o})")
                if member.issym():
                    target = member.linkname
                    resolved = posixpath.join(posixpath.dirname(name), target)
                    if target.startswith("/") or not _inside(resolved):
                        problems.append(f"{where}: symlink to {target!r}, outside the tree")
                    else:
                        links[name] = target
                elif member.islnk():
                    target = _normalised(member.linkname)
                    if member.linkname.startswith("/") or not _inside(target) or target not in seen:
                        problems.append(f"{where}: hard link to {member.linkname!r}, not an earlier member")
                elif not (member.isfile() or member.isdir()):
                    problems.append(f"{where}: unsupported member type {member.type!r}")
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError(f"{path}: not a readable tar archive: {exc}") from exc
    # Once every symlink is known: where each one, and each member's directory, really leads.
    for name, target in links.items():
        try:
            _resolve(posixpath.join(posixpath.dirname(name), target), links)
        except _Outside as exc:
            problems.append(f"{name!r}: symlink to {target!r}, outside the tree through the archive's symlinks ({exc})")
    for name in sorted(seen):
        try:
            _resolve(posixpath.dirname(name), links)
        except _Outside as exc:
            problems.append(f"{name!r}: its directory leads outside the tree through the archive's symlinks ({exc})")
    if problems:
        raise ReleaseError(f"{path}: unsafe to unpack into an install prefix:\n  " + "\n  ".join(problems))
    return count


def check_archive(path: Path, *, reserved: Collection[str] = ()) -> int:
    """:func:`check_tar` on the ``.tar.zst`` at ``path``, decompressed with ``zstd`` into a temporary file."""
    with tempfile.TemporaryDirectory(prefix="ci-release-archive-") as tmp:
        plain = Path(tmp) / "archive.tar"
        with plain.open("wb") as out:
            try:
                proc = subprocess.run(["zstd", "-dc", str(path)], stdout=out, stderr=subprocess.PIPE)
            except OSError as exc:
                raise ReleaseError(f"cannot run zstd to read {path}: {exc}") from exc
        if proc.returncode != 0:
            raise ReleaseError(f"{path}: zstd -d failed: {proc.stderr.decode(errors='replace').strip()}")
        return check_tar(plain, reserved=reserved)
