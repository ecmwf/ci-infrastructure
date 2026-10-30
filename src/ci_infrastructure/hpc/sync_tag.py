# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Sync and tag a release's HPC modules (CD v3, experimental): a dry run in milestone 1.

Ported from reusable-workflows v2 (``cd-actions/hpc-sync-tag``). ``modulemgr`` and ``software-sync``
are not safe to run concurrently, so every run, of any repo, takes the one lock
``<lock-root>/modulemgr`` (owner, heartbeat, stale recovery: ``transfer.remote_lock``), and
``<lock-root>/DISABLED`` stops them all.

Under the lock it checks that each leg's prefix holds the release promote installed, then prints
the commands it would run: sync each module (name, version) to the other clusters, and tag a
release (never a prerelease) ``new``. It holds the lock ``hold-seconds`` more (contention tests).
Running the commands is not part of milestone 1, so nothing touches the module tree.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Final

import yaml

from .._errors import CIError
from . import promote, release_tree, transfer
from .site import resolve_remote_path

#: Rendered into the commands: never anything a shell reads specially.
NAME_RE: Final = re.compile(r"^[A-Za-z0-9._+/-]+$")
TAG_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")
#: v2's default site; a site the cluster map does not list is treated as it.
DEFAULT_CLUSTER_SITE: Final = "aa-batch"
SOFTWARE_SYNC: Final = "/home/deploy/software-sync/bin/software-sync"
LOCK_NAME: Final = "modulemgr"


@dataclass
class Module:
    name: str
    version: str
    tag: str
    legs: list[dict[str, Any]] = field(default_factory=list)


def _check(what: str, value: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value):
        raise CIError(f"{what} {value!r} must match {pattern.pattern}")
    return value


def modules_of(legs_json: str | Mapping[str, Any], kind: str) -> list[Module]:
    """The (name, version) modules of ``kind``'s hpc-module legs in the gate's ``json`` output."""
    data = json.loads(legs_json) if isinstance(legs_json, str) else legs_json
    out: dict[tuple[str, str], Module] = {}
    for leg in ((data or {}).get(kind) or {}).get("include") or []:
        ctx = release_tree.release_context(leg)
        module = ctx.get("module") or {}
        name = _check("module name", str(module.get("name", "")), NAME_RE)
        version = _check("module version", str(module.get("version", "")), NAME_RE)
        tag = str(module.get("tag") or "")
        if tag:
            _check("module tag", tag, TAG_RE)
        m = out.setdefault((name, version), Module(name=name, version=version, tag=tag))
        if m.tag != tag:
            raise CIError(f"legs of module {name}/{version} disagree on its tag: {m.tag!r} and {tag!r}")
        m.legs.append(ctx)
    return list(out.values())


def load_clusters() -> dict[str, Any]:
    text = (resources.files("ci_infrastructure.hpc") / "module-clusters.yml").read_text()
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def commands(module: Module, *, site: str, clusters: Mapping[str, Any]) -> list[str]:
    """What v2 runs for one module on ``site``: sync to the other clusters, then (a release) tag."""
    key = site if site in clusters["sync_clusters"] else DEFAULT_CLUSTER_SITE
    sync = [str(c) for c in clusters["sync_clusters"].get(key) or []]
    tag_on = [str(c) for c in (clusters["tag_clusters"].get(key) or [])]
    out = ["module load modulemgr", f"modulemgr -v -f sync {module.name}"]
    if sync:
        out += [
            f"{SOFTWARE_SYNC} -s local -t {','.join(sync)} -p {module.name}",
            f"modulemgr -v -f -m {','.join(sync)} sync {module.name}",
        ]
    if module.tag and tag_on:
        out.append(f"modulemgr -m {','.join(tag_on)} -f -v tag {module.name} {module.version} {module.tag}")
    return out


def sync_tag(
    conn: Any,
    *,
    modules: Sequence[Module],
    site: str,
    lock_root: str,
    run_id: str,
    timeout: int = promote.DEFAULT_LOCK_TIMEOUT,
    stale_minutes: int = promote.DEFAULT_LOCK_STALE_MINUTES,
    poll: int = transfer.SHIP_LOCK_POLL_SECONDS,
    hold_seconds: int = 0,
    clusters: Mapping[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Dry run: under the global lock, check each promoted prefix and print the commands. Returns them."""
    clusters = clusters or load_clusters()
    if not modules:
        print("sync-tag: no HPC module in this release; nothing to do")
        return []
    promote.check_kill_switch(conn, lock_root)
    planned: list[str] = []
    with transfer.remote_lock(
        conn,
        lock_dir=f"{lock_root}/{LOCK_NAME}",
        run_id=run_id,
        what="sync/tag",
        subject=", ".join(f"{m.name}/{m.version}" for m in modules),
        timeout=timeout,
        poll=poll,
        stale_minutes=stale_minutes,
        heartbeat=transfer.LOCK_HEARTBEAT_SECONDS,
    ):
        promote.check_kill_switch(conn, lock_root)
        for module in modules:
            for ctx in module.legs:
                prefix = resolve_remote_path(conn, str(ctx["prefix"]))
                installed = release_tree.read_release_json(conn, prefix)
                if installed is None or not release_tree.same_release(installed, ctx):
                    raise CIError(
                        f"{prefix} does not hold leg {ctx.get('leg_id')} of {module.name}/{module.version} "
                        f"({release_tree.describe(installed or {})}): promote it first"
                    )
                print(f"sync-tag: {prefix} holds {module.name}/{module.version} (sha256 {installed.get('sha256')})")
            cmds = commands(module, site=site, clusters=clusters)
            print(f"::group::sync-tag (dry run, not run): {module.name}/{module.version}")
            for c in cmds:
                print(f"  {c}")
            print("::endgroup::")
            planned += cmds
        if hold_seconds > 0:
            print(f"sync-tag: holding the {LOCK_NAME} lock {hold_seconds}s (hold-seconds)")
            sleep(hold_seconds)
    return planned


def run_sync_tag(
    conn: Any,
    *,
    legs_json: str,
    kind: str,
    site: str,
    lock_root: str,
    timeout: int = promote.DEFAULT_LOCK_TIMEOUT,
    stale_minutes: int = promote.DEFAULT_LOCK_STALE_MINUTES,
    hold_seconds: int = 0,
    poll: int = transfer.SHIP_LOCK_POLL_SECONDS,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    modules = modules_of(legs_json, kind)
    if any(ctx.get("mode") == "rehearsal" for m in modules for ctx in m.legs):
        raise CIError("a rehearsal is never synced or tagged")
    root = resolve_remote_path(conn, promote.check_lock_root(lock_root)) if modules else ""
    return sync_tag(
        conn,
        modules=modules,
        site=site,
        lock_root=root,
        run_id=promote.run_id_from_env(env),
        timeout=timeout,
        stale_minutes=stale_minutes,
        hold_seconds=hold_seconds,
        poll=poll,
    )


def site_of(legs_json: str, kind: str) -> str:
    """The one troika site the kind's legs name."""
    sites = {str(leg.get("site") or "") for leg in _legs(legs_json, kind)}
    if len(sites) != 1 or "" in sites:
        raise CIError(f"the legs of {kind} name sites {sorted(sites)}: pass --site")
    return sites.pop()


def _legs(legs_json: str, kind: str) -> list[dict[str, Any]]:
    data = json.loads(legs_json)
    return list(((data or {}).get(kind) or {}).get("include") or [])
