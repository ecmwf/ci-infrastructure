#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""End each pull request of a sync branch with the list of all of them, in dependency order.

Run from pr-sync-branch.yml when one of them opens, reopens or closes; every listed pull request is
rewritten, so one event updates them all. Open and merged pull requests count, closed ones drop out;
with fewer than two the block is removed. A public repo's pull request lists only public repos' and says so.
The block sits after the Contributor Declaration, where check_pr_declaration ignores it.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import click

from ._errors import CIError
from ._github_api import _alias, _gh, select_token
from .default_branch_status import SEEDS, Edges, discover, ordered
from .sync_branch import is_sync_branch

BEGIN: Final = "<!-- SYNC-BRANCH_BEGIN -->"
END: Final = "<!-- SYNC-BRANCH_END -->"
HEADING: Final = "Synchronised rollout:"
PUBLIC_HEADING: Final = "Synchronised rollout (does not show synced PRs in internal repos):"
_BLOCK_RE: Final = re.compile(rf"\s*{re.escape(BEGIN)}.*?{re.escape(END)}", re.DOTALL)


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    url: str
    body: str
    public: bool


def query_for(branch: str) -> Callable[[Sequence[str]], str]:
    # A JSON string is a valid GraphQL string literal, so the branch name cannot break out of it.
    head = json.dumps(branch)

    def query(repos: Sequence[str]) -> str:
        parts = []
        for repo in repos:
            owner, name = repo.split("/", 1)
            parts.append(
                f'r_{_alias(repo)}: repository(owner: "{owner}", name: "{name}") {{\n'
                "  isPrivate\n"
                '  manifest: object(expression: "HEAD:.ci/manifest.toml") { ... on Blob { text } }\n'
                "  defaultBranchRef { name }\n"
                f"  pullRequests(headRefName: {head}, states: [OPEN, MERGED], first: 20) {{\n"
                "    nodes { number url body headRepository { nameWithOwner } }\n"
                "  }\n"
                "}"
            )
        return "{\n" + "\n".join(parts) + "\n}"

    return query


def involved(nodes: Mapping[str, Mapping[str, Any]], edges: Mapping[str, Edges]) -> list[PullRequest]:
    """The pull requests from each repo's own branch (not a fork's), upstream repos first, each once."""
    upstream = {repo: tuple(u for u in edges[repo].upstream if u in nodes) for repo in nodes}
    prs: dict[str, PullRequest] = {}
    for repo in ordered(upstream):
        node = nodes[repo]
        for pr in sorted(node["pullRequests"]["nodes"], key=lambda p: p["number"]):
            if (pr.get("headRepository") or {}).get("nameWithOwner") != repo:
                continue
            prs.setdefault(
                pr["url"], PullRequest(repo, pr["number"], pr["url"], pr["body"] or "", not node["isPrivate"])
            )
    return list(prs.values())


def render_block(urls: Sequence[str], public: bool = False) -> str:
    heading = PUBLIC_HEADING if public else HEADING
    return "\n".join([BEGIN, heading, "", *(f"- {url}" for url in urls), END])


def with_block(body: str, urls: Sequence[str], public: bool = False) -> str:
    """`body` ending with the block for `urls`, or without any block for fewer than two."""
    if len(urls) < 2:
        return _BLOCK_RE.sub("", body) if BEGIN in body else body
    return f"{_BLOCK_RE.sub('', body).rstrip()}\n\n{render_block(urls, public)}"


def updates(prs: Sequence[PullRequest]) -> list[tuple[PullRequest, str]]:
    """The pull requests whose description changes, with the new one."""
    out = []
    for pr in prs:
        urls = [p.url for p in prs if p.public or not pr.public]
        body = with_block(pr.body, urls, pr.public)
        if body != pr.body:
            out.append((pr, body))
    return out


def _patch_body(pr: PullRequest, body: str, token: str | None) -> bool:
    rc, _, err = _gh(
        ["gh", "api", "-X", "PATCH", f"repos/{pr.repo}/pulls/{pr.number}", "--input", "-", "--silent"],
        token,
        input_text=json.dumps({"body": body}),
    )
    if rc != 0:
        print(f"::error::{pr.url}: could not update the description: {err.strip()}", file=sys.stderr)
    return rc == 0


@click.command()
@click.option("--branch", required=True, help="The pull requests' head branch.")
@click.option("--dry-run", is_flag=True, help="Print the descriptions that would change instead of changing them.")
def main(branch: str, dry_run: bool) -> None:
    """List the pull requests of sync branch BRANCH at the end of each one's description."""
    if not is_sync_branch(branch):
        click.echo(f"{branch!r} is not a sync branch; nothing to list")
        return
    token = select_token()
    try:
        prs = involved(*discover(SEEDS, token, query_for(branch)))
    except CIError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{branch}: " + (", ".join(p.url for p in prs) or "no pull requests"))
    failed = False
    for pr, body in updates(prs):
        if dry_run:
            click.echo(f"would update {pr.url}:\n{body[-2000:]}\n")
        elif _patch_body(pr, body, token):
            click.echo(f"updated {pr.url}")
        else:
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
