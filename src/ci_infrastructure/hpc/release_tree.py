# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""An HPC release (hpc-module leg): the build's archive on the runner, and the tree under its prefix.

The build job (build-on-hpc ``publish: archive``) fetches the archive and its sidecars into one
directory, which is uploaded as the artifact ``release-<job_key>``:

- ``<filename>``: the prefix's tree, ``.tar.zst``;
- ``SHA256SUMS``: its digest; ``build-meta.json``: what was built, from what;
- ``itest-<leg_id>.json``: the in-job install test of exactly that digest;
- ``modules.txt``, ``build.log``.

When the release is installed under the prefix already, the build is skipped and the directory
holds only ``state.json`` (``already-published``). A promoted prefix holds ``.release.json``,
which names the release (package, tag, commit, leg, job key) and the digest it was unpacked from.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from .._errors import CIError
from . import jobscript

RELEASE_JSON: Final = ".release.json"
SHA256SUMS: Final = "SHA256SUMS"
BUILD_META: Final = "build-meta.json"
MODULES: Final = "modules.txt"
BUILD_LOG: Final = "build.log"
STATE_JSON: Final = "state.json"
STATE_BUILD: Final = "build"
STATE_ALREADY_PUBLISHED: Final = "already-published"

#: What makes two installs the same release; the digest says whether they are the same bytes.
IDENTITY: Final = ("package_name", "tag", "sha", "leg_id", "job_key")

State = Literal["build", "already-published"]


def itest_name(leg_id: str) -> str:
    return f"itest-{leg_id}.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_context(matrix_leg: str | Mapping[str, Any]) -> dict[str, Any]:
    """The leg's ``_release``, which must be an hpc-module release."""
    leg = json.loads(matrix_leg) if isinstance(matrix_leg, str) else matrix_leg
    ctx = leg.get("_release") if isinstance(leg, Mapping) else None
    if not isinstance(ctx, Mapping) or ctx.get("channel") != "hpc-module":
        raise CIError("an HPC release needs the matrix leg of an hpc-module release leg (with its _release context)")
    return dict(ctx)


def identity(ctx: Mapping[str, Any]) -> dict[str, Any]:
    return {k: ctx.get(k) for k in IDENTITY}


def read_release_json(conn: Any, prefix: str) -> dict[str, Any] | None:
    """``<prefix>/.release.json``; None when nothing is at ``prefix``. A prefix without one is refused."""
    q, q_json = shlex.quote(prefix), shlex.quote(f"{prefix}/{RELEASE_JSON}")
    script = "\n".join(
        [
            f"if [ ! -e {q} ] && [ ! -L {q} ]; then exit 3; fi",
            # Never read through a link: the file promote writes is a regular file.
            f"if [ -L {q_json} ]; then echo '{RELEASE_JSON} is a symlink' >&2; exit 4; fi",
            f"cat {q_json}",
        ]
    )
    proc = conn.execute(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if proc.returncode == 3:
        return None
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)
    try:
        data = json.loads(text) if proc.returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        detail = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr or "").strip()
        raise CIError(
            f"{prefix} exists but has no readable {RELEASE_JSON} ({detail or 'not JSON'}): not a tree a release "
            "installed; it is never overwritten. Remove it by hand if it is debris."
        )
    return data


def same_release(existing: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    return identity(existing) == identity(ctx)


def describe(release: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={release.get(k)}" for k in (*IDENTITY, "sha256"))


def probe_prefix(conn: Any, prefix: str, ctx: Mapping[str, Any]) -> tuple[State, dict[str, Any] | None]:
    """``build`` when nothing is at ``prefix``; ``already-published`` (with its ``.release.json``) when this
    release is; refused when another release, or anything else, is there."""
    existing = read_release_json(conn, prefix)
    if existing is None:
        return STATE_BUILD, None
    if not same_release(existing, ctx):
        raise CIError(
            f"{prefix} holds another release ({describe(existing)}), not {describe(ctx)}: versions are "
            "immutable; tag a new rc"
        )
    return STATE_ALREADY_PUBLISHED, existing


@dataclass(frozen=True)
class LocalBuild:
    """What the build job handed over: the archive and its digest, or (already published) neither."""

    state: State
    archive: Path | None = None
    sha256: str = ""
    build_log: Path | None = None
    installed: dict[str, Any] | None = None


def write_already_published(directory: Path, existing: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    state = {"state": STATE_ALREADY_PUBLISHED, "release": dict(existing)}
    (directory / STATE_JSON).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _load_json(path: Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CIError(f"{what} ({path}) is missing or not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CIError(f"{what} ({path}) is not a JSON object")
    return data


def check_local_build(directory: Path, ctx: Mapping[str, Any]) -> LocalBuild:
    """The build of this leg in ``directory``, checked: the archive's digest is the one written beside it
    and the one its install test passed, for this commit and job key. Raises on any mismatch."""
    state_file = directory / STATE_JSON
    archive = directory / str(ctx["filename"])
    if state_file.is_file() and not archive.exists():
        state = _load_json(state_file, "the build's state")
        installed = state.get("release")
        if state.get("state") != STATE_ALREADY_PUBLISHED or not isinstance(installed, dict):
            raise CIError(f"{state_file}: not an already-published state")
        if not same_release(installed, ctx):
            raise CIError(f"{state_file} names another release ({describe(installed)})")
        return LocalBuild(state=STATE_ALREADY_PUBLISHED, installed=installed)
    if not archive.is_file():
        raise CIError(f"{directory} holds no {archive.name} (and no {STATE_JSON}): the build job handed over nothing")
    digest = sha256_file(archive)
    try:
        written = (directory / SHA256SUMS).read_text().split()[0]
    except (OSError, IndexError) as exc:
        raise CIError(f"{directory / SHA256SUMS} is missing or empty") from exc
    if written != digest:
        raise CIError(f"{archive.name}: sha256 {digest} is not the {written} the build wrote")
    itest = _load_json(directory / itest_name(str(ctx["leg_id"])), "the install test receipt")
    if itest.get("result") != "passed":
        raise CIError(f"the install test of {archive.name} did not pass: {itest.get('result')!r}")
    if itest.get("sha256") != digest:
        raise CIError(f"the install test passed sha256 {itest.get('sha256')}, not this archive's {digest}")
    if (itest.get("job_key"), itest.get("sha")) != (ctx.get("job_key"), ctx.get("sha")):
        raise CIError(
            f"the install test is of job {itest.get('job_key')} at {itest.get('sha')}, "
            f"not {ctx.get('job_key')} at {ctx.get('sha')}"
        )
    meta = _load_json(directory / BUILD_META, "the build metadata")
    if (meta.get("release") or {}).get("job_key") != ctx.get("job_key"):
        raise CIError(f"{BUILD_META} is not of job {ctx.get('job_key')}")
    log = directory / BUILD_LOG
    return LocalBuild(state=STATE_BUILD, archive=archive, sha256=digest, build_log=log if log.is_file() else None)


def fetch_archive(conn: Any, *, remote_install_dir: str, local_dir: Path, ctx: Mapping[str, Any]) -> LocalBuild:
    """Fetch, never unpack, the archive the release job wrote at ``CI_INSTALL_ARCHIVE`` and its sidecars
    into ``local_dir``, then :func:`check_local_build` it."""
    remote = jobscript.install_archive_path(remote_install_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    pulls = [
        (remote, str(ctx["filename"])),
        (f"{remote}.sha256", f"{ctx['filename']}.sha256"),
        (f"{remote}.itest.json", itest_name(str(ctx["leg_id"]))),
        (f"{remote}.build-meta.json", BUILD_META),
        (f"{remote}.modules", MODULES),
    ]
    for src, name in pulls:
        try:
            conn.getfile(src, local_dir / name)
        except Exception as exc:
            what = (
                "no install test receipt: the in-job install test did not finish" if src.endswith(".itest.json") else ""
            )
            raise CIError(f"cannot fetch {src}{': ' + what if what else ''} ({exc})") from exc
    sidecar = local_dir / f"{ctx['filename']}.sha256"
    written = sidecar.read_text().strip()
    sidecar.unlink()
    (local_dir / SHA256SUMS).write_text(f"{written}  {ctx['filename']}\n")
    return check_local_build(local_dir, ctx)
