#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
"""Registry repositories that no Dockerfile in this repo builds any more.

An orphan keeps serving `:latest`, so a reference to a renamed or deleted image
keeps working silently instead of failing -- until the image goes stale. Nothing
in images.yml removes them, hence this.

    scripts/registry_orphans.py                 # report
    scripts/registry_orphans.py --format md     # markdown, for a job summary
    scripts/registry_orphans.py --delete        # report, then delete

Reading is anonymous. --delete needs PUBLIC_ECCR_ROBOT_NAME / _TOKEN with delete
permission on the project. Exit 0 whether or not orphans were found.

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


def _request(method: str, url: str, *, auth: tuple[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, method=method)  # noqa: S310 -- https, fixed host
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read()
    return json.loads(body) if body else None


def list_repositories() -> list[dict[str, Any]]:
    """Every repository in the project, following Harbor's pagination."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        got = _request("GET", f"{API}/projects/{PROJECT}/repositories?page_size={PAGE_SIZE}&page={page}")
        if not got:
            return out
        out.extend(got)
        if len(got) < PAGE_SIZE:
            return out
        page += 1


def short_name(repository: str) -> str:
    """Harbor reports "<project>/<name>"; the name alone is what a Dockerfile maps to."""
    return repository.split("/", 1)[1] if "/" in repository else repository


def find_orphans(built: Iterable[str], repositories: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Repositories with no matching image, newest-touched first."""
    names = {flat_name(name) for name in built}
    orphans = [dict(r) for r in repositories if short_name(str(r["name"])) not in names]
    return sorted(orphans, key=lambda r: str(r.get("update_time", "")), reverse=True)


def delete_repository(name: str, auth: tuple[str, str]) -> None:
    path = urllib.parse.quote(name, safe="")
    _request("DELETE", f"{API}/projects/{PROJECT}/repositories/{path}", auth=auth)


def render(orphans: Sequence[Mapping[str, Any]], fmt: str) -> str:
    if not orphans:
        return "No orphans: every repository in the registry is still built here."
    rows = [
        (
            short_name(str(o["name"])),
            str(o.get("artifact_count", "?")),
            str(o.get("pull_count", "?")),
            str(o.get("update_time", ""))[:10],
        )
        for o in orphans
    ]
    if fmt == "md":
        head = [
            f"**{len(rows)} orphaned repositories** in `{REGISTRY}/{PROJECT}`.",
            "",
            "| repository | tags | pulls | last update |",
            "|---|---|---|---|",
        ]
        return "\n".join(head + [f"| `{n}` | {t} | {p} | {u} |" for n, t, p, u in rows])
    width = max(len(r[0]) for r in rows)
    head = [f"{len(rows)} orphaned repositories in {REGISTRY}/{PROJECT}:", ""]
    return "\n".join(head + [f"  {n:<{width}}  tags={t:<4} pulls={p:<6} last update {u}" for n, t, p, u in rows])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delete", action="store_true", help="delete the orphans after reporting them")
    ap.add_argument("--format", choices=("text", "md"), default="text")
    args = ap.parse_args(argv)

    orphans = find_orphans(enumerate_images(), list_repositories())
    print(render(orphans, args.format))
    if not args.delete or not orphans:
        return 0

    name, token = os.environ.get("PUBLIC_ECCR_ROBOT_NAME", ""), os.environ.get("PUBLIC_ECCR_ROBOT_TOKEN", "")
    if not name or not token:
        print("::error::--delete needs PUBLIC_ECCR_ROBOT_NAME and PUBLIC_ECCR_ROBOT_TOKEN", file=sys.stderr)
        return 2
    failed = 0
    print("", file=sys.stderr)
    for orphan in orphans:
        repo = short_name(str(orphan["name"]))
        try:
            delete_repository(repo, (name, token))
            print(f"deleted {repo}", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            failed += 1
            print(f"::error::could not delete {repo}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
