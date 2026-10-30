# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Sync and tag a release's HPC modules (CD v3, experimental): a dry run in milestone 1.

Ported from reusable-workflows v2 (``cd-actions/hpc-sync-tag``). ``modulemgr`` and ``software-sync``
are not safe to run concurrently, so every run, of any repo, takes the one lock
``<lock-root>/modulemgr`` (owner, heartbeat, stale recovery: ``transfer.remote_lock``), and
``<lock-root>/DISABLED`` stops them all.

Which clusters a module is synced to and tagged on comes from ``module-clusters.yml``, keyed by the
*cluster site*: the troika site the module was built and promoted on, or the one ``--cluster-site``
names. Unlike v2 nothing is guessed:

- a cluster site the map does not list (e.g. ``hpc-batch``, which lands on any cluster) is refused,
  never treated as ``aa-batch``;
- nothing is synced from or to, or tagged on, ``ag`` (:data:`FORBIDDEN_CLUSTERS`): a cluster site
  on it, or a plan naming it, is refused;
- under the lock the cluster the connection runs on is read from its ``hostname`` (``ac6-115.bullx``
  is ``ac``; ``$EC_CLUSTER``, when set, must agree), and must be the cluster site's cluster.

Under the lock it then checks that each leg's prefix holds the release promote installed, and prints
the commands it would run: sync each module (name, version) to the other clusters, and tag a release
(never a prerelease) ``new``. It holds the lock ``hold-seconds`` more (contention tests). Running the
commands is not part of milestone 1: nothing runs ``software-sync`` or ``modulemgr``.

Open item: the cluster map (sites, their clusters, the hostname rule) is ported from v2 and must be
confirmed with the HPC admins before a real sync is enabled.
"""

from __future__ import annotations

import json
import re
import subprocess
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
SOFTWARE_SYNC: Final = "/home/deploy/software-sync/bin/software-sync"
LOCK_NAME: Final = "modulemgr"
#: Never synced from or to, nor tagged on.
FORBIDDEN_CLUSTERS: Final = frozenset({"ag"})
#: An Atos node: ``<cluster><n>-<n>[.domain]``, e.g. ``ac6-115.bullx``.
HOST_RE: Final = re.compile(r"^([a-z]{2})[0-9]+-[0-9]+(?:\.|$)")


@dataclass
class Module:
    name: str
    version: str
    tag: str
    legs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Plan:
    """Where a module built on ``cluster_site`` (cluster ``source``) is synced to and tagged on."""

    cluster_site: str
    source: str
    sync: list[str]
    tag_on: list[str]


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


def plan_for(cluster_site: str, clusters: Mapping[str, Any]) -> Plan:
    """The cluster site's plan; refused for a site the map does not list, or one touching ``ag``."""
    site_cluster = clusters.get("site_cluster") or {}
    listed = sorted(
        set(site_cluster) & set(clusters.get("sync_clusters") or {}) & set(clusters.get("tag_clusters") or {})
    )
    if cluster_site not in listed:
        raise CIError(
            f"sync-tag: cluster site {cluster_site!r} is not in module-clusters.yml (listed: {listed}), so which "
            "cluster the module is on, and where it would be synced, is unknown; nothing is guessed. Build and "
            "promote on a listed site, or name it with cluster-site"
        )
    source = str(site_cluster[cluster_site])
    sync = [str(c) for c in clusters["sync_clusters"].get(cluster_site) or []]
    tag_on = [str(c) for c in clusters["tag_clusters"].get(cluster_site) or []]
    if source in FORBIDDEN_CLUSTERS:
        raise CIError(f"sync-tag: cluster site {cluster_site!r} is on {source}: nothing is synced or tagged from it")
    for what, targets in (("sync", sync), ("tag", tag_on)):
        if bad := sorted(FORBIDDEN_CLUSTERS & set(targets)):
            raise CIError(f"sync-tag: the {what} targets of {cluster_site!r} {targets} name {bad}: never touched")
    return Plan(cluster_site=cluster_site, source=source, sync=sync, tag_on=tag_on)


def commands(module: Module, plan: Plan) -> list[str]:
    """What v2 runs for one module: sync to the other clusters, then (a release) tag."""
    out = ["module load modulemgr", f"modulemgr -v -f sync {module.name}"]
    if plan.sync:
        out += [
            f"{SOFTWARE_SYNC} -s local -t {','.join(plan.sync)} -p {module.name}",
            f"modulemgr -v -f -m {','.join(plan.sync)} sync {module.name}",
        ]
    if module.tag and plan.tag_on:
        out.append(f"modulemgr -m {','.join(plan.tag_on)} -f -v tag {module.name} {module.version} {module.tag}")
    return out


def cluster_of_host(hostname: str, ec_cluster: str = "") -> str:
    """``ac6-115.bullx`` -> ``ac``; refused for any other name, or when ``$EC_CLUSTER`` disagrees."""
    m = HOST_RE.match(hostname)
    if not m:
        raise CIError(f"sync-tag: cannot tell the cluster from the hostname {hostname!r} (expected e.g. ac6-115.bullx)")
    cluster = m.group(1)
    if ec_cluster and ec_cluster != cluster:
        raise CIError(f"sync-tag: the hostname {hostname!r} is on {cluster}, but EC_CLUSTER says {ec_cluster!r}")
    return cluster


def detect_cluster(conn: Any) -> str:
    """The cluster the connection's commands run on (login shell: ``$EC_CLUSTER`` is set in profile.d)."""
    proc = conn.execute(
        ["bash", "-lc", 'printf "%s\\n%s\\n" "$(hostname)" "${EC_CLUSTER:-}"'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout, _stderr = proc.communicate()
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
    if proc.returncode != 0:
        raise CIError(f"sync-tag: cannot tell which cluster this is: hostname failed (exit {proc.returncode})")
    lines = [*text.splitlines(), "", ""]
    return cluster_of_host(lines[0].strip(), lines[1].strip())


def check_cluster(conn: Any, plan: Plan, detect: Callable[[Any], str]) -> str:
    """Refuse unless the connection runs on the plan's source cluster, and that is not ``ag``."""
    on = detect(conn)
    if on in FORBIDDEN_CLUSTERS:
        raise CIError(f"sync-tag: this connection runs on {on}: nothing is synced or tagged from it")
    if on != plan.source:
        raise CIError(
            f"sync-tag: this connection runs on {on}, but cluster site {plan.cluster_site!r} is {plan.source}: "
            "the commands would sync another cluster's module tree"
        )
    return on


def sync_tag(
    conn: Any,
    *,
    modules: Sequence[Module],
    cluster_site: str,
    lock_root: str,
    run_id: str,
    timeout: int = promote.DEFAULT_LOCK_TIMEOUT,
    stale_minutes: int = promote.DEFAULT_LOCK_STALE_MINUTES,
    poll: int = transfer.SHIP_LOCK_POLL_SECONDS,
    hold_seconds: int = 0,
    clusters: Mapping[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    detect: Callable[[Any], str] = detect_cluster,
) -> list[str]:
    """Dry run: under the global lock, check the cluster and each promoted prefix, print the commands."""
    clusters = clusters or load_clusters()
    if not modules:
        print("sync-tag: no HPC module in this release; nothing to do")
        return []
    plan = plan_for(cluster_site, clusters)
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
        on = check_cluster(conn, plan, detect)
        print(f"sync-tag: on cluster {on} (cluster site {plan.cluster_site})")
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
            cmds = commands(module, plan)
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
    cluster_site: str,
    lock_root: str,
    timeout: int = promote.DEFAULT_LOCK_TIMEOUT,
    stale_minutes: int = promote.DEFAULT_LOCK_STALE_MINUTES,
    hold_seconds: int = 0,
    poll: int = transfer.SHIP_LOCK_POLL_SECONDS,
    env: Mapping[str, str] | None = None,
    detect: Callable[[Any], str] = detect_cluster,
) -> list[str]:
    modules = modules_of(legs_json, kind)
    if any(ctx.get("mode") == "rehearsal" for m in modules for ctx in m.legs):
        raise CIError("a rehearsal is never synced or tagged")
    if modules:
        plan_for(cluster_site, load_clusters())  # refuse before touching the cluster
    root = resolve_remote_path(conn, promote.check_lock_root(lock_root)) if modules else ""
    return sync_tag(
        conn,
        modules=modules,
        cluster_site=cluster_site,
        lock_root=root,
        run_id=promote.run_id_from_env(env),
        timeout=timeout,
        stale_minutes=stale_minutes,
        hold_seconds=hold_seconds,
        poll=poll,
        detect=detect,
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
