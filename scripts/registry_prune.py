#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
"""Delete all but the newest image versions in every repository of the project.

Every push adds a version, and nothing else removes one, so the project quota
fills up and pushes start failing with "exceed the configured upper limit".

    scripts/registry_prune.py                 # report what would go
    scripts/registry_prune.py --format md     # markdown, for a job summary
    scripts/registry_prune.py --delete        # report, then delete
    scripts/registry_prune.py --keep 3        # keep three per repository

Per repository the --keep most recently pushed artifacts stay, and so does
anything tagged `latest` whatever its age. Reading is anonymous; --delete needs
PUBLIC_ECCR_CLEANUP_ROBOT_NAME / _TOKEN, a robot with artifact-delete permission.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, Final

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_image import PROJECT, REGISTRY  # noqa: E402
from registry_orphans import API, PAGE_SIZE, _request, list_repositories, short_name  # noqa: E402

DEFAULT_KEEP: Final = 2
PROTECTED_TAGS: Final = frozenset({"latest"})


def _artifacts_url(repo: str) -> str:
    return f"{API}/projects/{PROJECT}/repositories/{urllib.parse.quote(repo, safe='')}/artifacts"


def list_artifacts(repo: str) -> list[dict[str, Any]]:
    """Every artifact of one repository, with its tags."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        got = _request("GET", f"{_artifacts_url(repo)}?with_tag=true&page_size={PAGE_SIZE}&page={page}")
        if not got:
            return out
        out.extend(got)
        if len(got) < PAGE_SIZE:
            return out
        page += 1


def tag_names(artifact: Mapping[str, Any]) -> list[str]:
    return [str(t["name"]) for t in artifact.get("tags") or []]


def stale(artifacts: Sequence[Mapping[str, Any]], keep: int) -> list[dict[str, Any]]:
    """The artifacts to delete: all but the `keep` newest, never one tagged `latest`."""
    newest_first = sorted(artifacts, key=lambda a: str(a.get("push_time", "")), reverse=True)
    return [dict(a) for a in newest_first[keep:] if not PROTECTED_TAGS & set(tag_names(a))]


def delete_artifact(repo: str, digest: str, auth: tuple[str, str]) -> None:
    _request("DELETE", f"{_artifacts_url(repo)}/{digest}", auth=auth)


def render(plan: Mapping[str, Sequence[Mapping[str, Any]]], keep: int, fmt: str) -> str:
    rows = [
        (repo, ",".join(tag_names(a)) or "(untagged)", str(a.get("push_time", ""))[:10], int(a.get("size") or 0))
        for repo, items in plan.items()
        for a in items
    ]
    if not rows:
        return f"Nothing to prune: no repository in {REGISTRY}/{PROJECT} holds more than {keep} versions."
    total = sum(r[3] for r in rows) / 2**30
    summary = (
        f"{len(rows)} versions in {len(plan)} repositories of {REGISTRY}/{PROJECT} are older than "
        f"the newest {keep} ({total:.1f} GiB before layer sharing)"
    )
    if fmt == "md":
        head = [f"**{summary}.**", "", "| repository | tags | pushed |", "|---|---|---|"]
        return "\n".join(head + [f"| `{r}` | {t} | {p} |" for r, t, p, _ in rows])
    width = max(len(r[0]) for r in rows)
    return "\n".join([f"{summary}:", ""] + [f"  {r:<{width}}  {p}  {t}" for r, t, p, _ in rows])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP, help=f"versions to keep per repository (default {DEFAULT_KEEP})"
    )
    ap.add_argument("--delete", action="store_true", help="delete the stale versions after reporting them")
    ap.add_argument("--format", choices=("text", "md"), default="text")
    args = ap.parse_args(argv)
    if args.keep < 1:
        ap.error("--keep must be at least 1")

    plan: dict[str, list[dict[str, Any]]] = {}
    for repository in list_repositories():
        repo = short_name(str(repository["name"]))
        doomed = stale(list_artifacts(repo), args.keep)
        if doomed:
            plan[repo] = doomed
    print(render(plan, args.keep, args.format))
    if not args.delete or not plan:
        return 0

    name, token = (
        os.environ.get("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", ""),
        os.environ.get("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", ""),
    )
    if not name or not token:
        print(
            "::error::--delete needs PUBLIC_ECCR_CLEANUP_ROBOT_NAME and PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN",
            file=sys.stderr,
        )
        return 2
    failed = 0
    print("", file=sys.stderr)
    for repo, doomed in plan.items():
        for artifact in doomed:
            label = f"{repo}@{','.join(tag_names(artifact)) or artifact['digest']}"
            try:
                delete_artifact(repo, str(artifact["digest"]), (name, token))
                print(f"deleted {label}", file=sys.stderr)
            except urllib.error.HTTPError as exc:
                failed += 1
                print(f"::error::could not delete {label}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
