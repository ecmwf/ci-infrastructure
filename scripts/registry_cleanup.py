#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
"""Clean up the registry project: old versions, and repositories nothing builds.

    scripts/registry_cleanup.py prune             # versions older than the newest 2
    scripts/registry_cleanup.py prune --keep 3    # ... the newest 3
    scripts/registry_cleanup.py orphans           # repositories no Dockerfile here builds
    scripts/registry_cleanup.py <task> --delete   # report, then delete
    scripts/registry_cleanup.py <task> --format md

prune keeps the project under quota ("exceed the configured upper limit"); it
never deletes a version tagged `latest`. orphans are repositories whose
Dockerfile is gone but which still serve a stale `:latest`.

Reading is anonymous. --delete needs PUBLIC_ECCR_CLEANUP_ROBOT_NAME / _TOKEN.

Env overrides: REGISTRY, PROJECT, IMAGES_DIR (shared with build_image.py).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_image import PROJECT, REGISTRY, enumerate_images, flat_name  # noqa: E402

API: Final = f"https://{REGISTRY}/api/v2.0"
PAGE_SIZE: Final = 100
DEFAULT_KEEP: Final = 2
PROTECTED_TAGS: Final = frozenset({"latest"})


def _request(method: str, url: str, *, auth: tuple[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, method=method)  # noqa: S310 -- https, fixed host
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read()
    return json.loads(body) if body else None


def _paged(url: str) -> list[dict[str, Any]]:
    """Every item behind a Harbor list endpoint, following its pagination."""
    sep = "&" if "?" in url else "?"
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        got = _request("GET", f"{url}{sep}page_size={PAGE_SIZE}&page={page}")
        if not got:
            return out
        out.extend(got)
        if len(got) < PAGE_SIZE:
            return out
        page += 1


def _repo_url(repo: str) -> str:
    return f"{API}/projects/{PROJECT}/repositories/{urllib.parse.quote(repo, safe='')}"


def list_repositories() -> list[dict[str, Any]]:
    return _paged(f"{API}/projects/{PROJECT}/repositories")


def list_artifacts(repo: str) -> list[dict[str, Any]]:
    return _paged(f"{_repo_url(repo)}/artifacts?with_tag=true")


def short_name(repository: str) -> str:
    """ "<project>/<name>" -> "<name>"."""
    return repository.split("/", 1)[1] if "/" in repository else repository


def tag_names(artifact: Mapping[str, Any]) -> list[str]:
    return [str(t["name"]) for t in artifact.get("tags") or []]


# --- what is stale -----------------------------------------------------------


def find_orphans(built: Iterable[str], repositories: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Repositories with no matching image, newest-touched first."""
    names = {flat_name(name) for name in built}
    orphans = [dict(r) for r in repositories if short_name(str(r["name"])) not in names]
    return sorted(orphans, key=lambda r: str(r.get("update_time", "")), reverse=True)


def stale_versions(artifacts: Sequence[Mapping[str, Any]], keep: int) -> list[dict[str, Any]]:
    """All but the `keep` newest by push time, never one tagged `latest`."""
    newest_first = sorted(artifacts, key=lambda a: str(a.get("push_time", "")), reverse=True)
    return [dict(a) for a in newest_first[keep:] if not PROTECTED_TAGS & set(tag_names(a))]


# --- one row per deletion: (label, report cells, URL to DELETE) ------------------

Doomed = tuple[str, tuple[str, ...], str]


def plan_orphans() -> list[Doomed]:
    out: list[Doomed] = []
    for o in find_orphans(enumerate_images(), list_repositories()):
        repo = short_name(str(o["name"]))
        cells = (
            f"`{repo}`",
            str(o.get("artifact_count", "?")),
            str(o.get("pull_count", "?")),
            str(o.get("update_time", ""))[:10],
        )
        out.append((repo, cells, _repo_url(repo)))
    return out


def plan_prune(keep: int) -> list[Doomed]:
    out: list[Doomed] = []
    for r in list_repositories():
        repo = short_name(str(r["name"]))
        for a in stale_versions(list_artifacts(repo), keep):
            tags = ",".join(tag_names(a)) or "(untagged)"
            cells = (f"`{repo}`", tags, str(a.get("push_time", ""))[:10])
            out.append((f"{repo}@{tags}", cells, f"{_repo_url(repo)}/artifacts/{a['digest']}"))
    return out


HEADERS: Final = {"orphans": ("repository", "tags", "pulls", "last update"), "prune": ("repository", "tags", "pushed")}


def render(task: str, rows: Sequence[Doomed], fmt: str, keep: int = DEFAULT_KEEP) -> str:
    where = f"{REGISTRY}/{PROJECT}"
    if not rows:
        if task == "orphans":
            return f"No orphans: every repository in {where} is still built here."
        return f"Nothing to prune: no repository in {where} holds more than {keep} versions."
    title = (
        f"{len(rows)} orphaned repositories in {where}"
        if task == "orphans"
        else f"{len(rows)} versions in {where} older than the newest {keep} of their repository"
    )
    if fmt == "md":
        head = HEADERS[task]
        lines = [f"**{title}.**", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        return "\n".join(lines + ["| " + " | ".join(cells) + " |" for _, cells, _ in rows])
    table = [[c.strip("`") for c in cells] for _, cells, _ in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    return "\n".join(
        [f"{title}:", ""] + ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    )


def delete(rows: Sequence[Doomed]) -> int:
    name = os.environ.get("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", "")
    token = os.environ.get("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", "")
    if not name or not token:
        print("::error::--delete needs PUBLIC_ECCR_CLEANUP_ROBOT_NAME and _TOKEN", file=sys.stderr)
        return 2
    failed = 0
    print("", file=sys.stderr)
    for label, _, url in rows:
        try:
            _request("DELETE", url, auth=(name, token))
            print(f"deleted {label}", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            failed += 1
            print(f"::error::could not delete {label}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", choices=("prune", "orphans"))
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"prune: versions to keep (default {DEFAULT_KEEP})")
    ap.add_argument("--delete", action="store_true", help="delete after reporting")
    ap.add_argument("--format", choices=("text", "md"), default="text")
    args = ap.parse_args(argv)
    if args.keep < 1:
        ap.error("--keep must be at least 1")

    rows = plan_orphans() if args.task == "orphans" else plan_prune(args.keep)
    print(render(args.task, rows, args.format, args.keep))
    if not args.delete or not rows:
        return 0
    return delete(rows)


if __name__ == "__main__":
    raise SystemExit(main())
