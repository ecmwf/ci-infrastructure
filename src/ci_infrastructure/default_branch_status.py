#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Dashboard of the default branches of the rolled-out repos: their CI, who broke it, who pushed without a PR.

The repos are found from ecbuild along the manifests' `[[deps]]` and `[[trigger-downstream]]`.
Writes the dashboard to $GITHUB_STEP_SUMMARY and fails when a repo's default branch is red or blocked.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Final, Literal

import click

from . import manifest
from ._errors import CIError
from ._github_api import IN_PROGRESS_STATUSES, ManifestSchemaError, _alias, gh_api_graphql, gh_api_rest, select_token

#: Where discovery starts: the bottom of the stack, whose manifest names its consumers.
SEEDS: Final = ("ecmwf/ecbuild",)
CI_WORKFLOW: Final = "ci.yml"
_HISTORY: Final = 100  # commits fetched to walk the first-parent chain
_TIMELINE: Final = 15  # first-parent commits shown per repo
_DIRECT_PUSH_WINDOW: Final = timedelta(days=14)

_RED: Final = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
# A cancelled or skipped run says nothing about the commit; the verdict looks further back.
_NO_VERDICT: Final = frozenset({"cancelled", "skipped", "neutral", "stale"})

Verdict = Literal["green", "red", "unknown"]


@dataclass(frozen=True)
class Run:
    sha: str
    url: str
    conclusion: str | None  # None while it runs
    pusher: str


@dataclass(frozen=True)
class Commit:
    sha: str
    url: str
    headline: str
    author: str
    pr: tuple[int, str] | None  # (number, url) of the merged PR that brought it in
    run: Run | None
    date: datetime


@dataclass(frozen=True)
class RepoState:
    repo: str
    branch: str
    upstream: tuple[str, ...]
    commits: tuple[Commit, ...]
    runs: tuple[Run, ...]  # newest first

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]

    @property
    def last_completed(self) -> Run | None:
        return next((r for r in self.runs if r.conclusion and r.conclusion not in _NO_VERDICT), None)

    @property
    def verdict(self) -> Verdict:
        run = self.last_completed
        if run is None:
            return "unknown"
        return "red" if run.conclusion in _RED else "green"


@dataclass(frozen=True)
class Status:
    icon: str
    text: str
    ok: bool


def _query(repos: Sequence[str]) -> str:
    parts = []
    for repo in repos:
        owner, name = repo.split("/", 1)
        parts.append(
            f'r_{_alias(repo)}: repository(owner: "{owner}", name: "{name}") {{\n'
            '  manifest: object(expression: "HEAD:.ci/manifest.toml") { ... on Blob { text } }\n'
            "  defaultBranchRef { name target { ... on Commit {\n"
            f"    history(first: {_HISTORY}) {{ nodes {{\n"
            "      oid url messageHeadline committedDate\n"
            "      parents(first: 1) { nodes { oid } }\n"
            "      author { name user { login } }\n"
            "      associatedPullRequests(first: 10) { nodes { number url merged baseRefName } }\n"
            "    } }\n"
            "  } } }\n"
            "}"
        )
    return "{\n" + "\n".join(parts) + "\n}"


@dataclass(frozen=True)
class Edges:
    upstream: tuple[str, ...]  # repos it depends on, in any of its packages
    downstream: tuple[str, ...]  # its [[trigger-downstream]] repos


def edges_of(manifest_text: str, repo: str) -> Edges:
    """The other repos `repo`'s manifest names."""
    try:
        m = manifest.validate(tomllib.loads(manifest_text))
    except (tomllib.TOMLDecodeError, ManifestSchemaError) as exc:
        print(f"::warning::{repo}: unreadable manifest, no edges: {exc}", file=sys.stderr)
        return Edges((), ())
    deps = [*m.deps, *(d for entry in m.packages.values() for d in entry.deps)]
    upstream = dict.fromkeys(d.repo for d in deps if d.repo and d.repo != repo)
    return Edges(tuple(upstream), tuple(dict.fromkeys(t.repo for t in m.trigger_downstream)))


def first_parent(nodes: Sequence[Mapping[str, Any]], limit: int | None = _TIMELINE) -> list[Mapping[str, Any]]:
    """The first-parent chain from the head; the commits a merged PR brought along are skipped."""
    by_oid = {n["oid"]: n for n in nodes}
    chain: list[Mapping[str, Any]] = []
    node = nodes[0] if nodes else None
    while node is not None and (limit is None or len(chain) < limit):
        chain.append(node)
        parents = node["parents"]["nodes"]
        node = by_oid.get(parents[0]["oid"]) if parents else None
    return chain


def _merged_pr(node: Mapping[str, Any], branch: str) -> tuple[int, str] | None:
    """The merged PR into `branch` that brought the commit in, whichever way it was merged."""
    for pr in node["associatedPullRequests"]["nodes"]:
        if pr["merged"] and pr["baseRefName"] == branch:
            return int(pr["number"]), str(pr["url"])
    return None


def _runs(repo: str, branch: str, token: str | None) -> tuple[Run, ...]:
    data = gh_api_rest(
        f"repos/{repo}/actions/workflows/{CI_WORKFLOW}/runs?branch={branch}&per_page=100&exclude_pull_requests=true",
        token,
    )
    if not isinstance(data, dict):
        print(f"::warning::{repo}: could not list the {CI_WORKFLOW} runs on {branch}", file=sys.stderr)
        return ()
    return tuple(
        Run(
            sha=r["head_sha"],
            url=r["html_url"],
            conclusion=None if r["status"] in IN_PROGRESS_STATUSES else r["conclusion"],
            pusher=(r.get("actor") or {}).get("login", ""),
        )
        for r in data.get("workflow_runs", [])
        if r["event"] != "pull_request"
    )


def repo_state(repo: str, data: Mapping[str, Any], runs: tuple[Run, ...], upstream: tuple[str, ...]) -> RepoState:
    ref = data["defaultBranchRef"]
    branch = ref["name"]
    run_of: dict[str, Run] = {}
    for run in runs:  # newest first: a re-run commit keeps its latest run
        run_of.setdefault(run.sha, run)
    commits = tuple(
        Commit(
            sha=n["oid"],
            url=n["url"],
            headline=n["messageHeadline"],
            author=(n["author"]["user"] or {}).get("login") or n["author"]["name"] or "",
            pr=_merged_pr(n, branch),
            run=run_of.get(n["oid"]),
            date=datetime.fromisoformat(n["committedDate"]),
        )
        for n in first_parent(ref["target"]["history"]["nodes"], limit=None)
    )
    return RepoState(repo, branch, upstream, commits, runs)


def discover(
    seeds: Sequence[str], token: str | None, query: Callable[[Sequence[str]], str] = _query
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Edges]]:
    """Every repo with a manifest reachable from `seeds`, with its `query` node and edges.

    `query` must select `manifest` and `defaultBranchRef` like `_query`.
    """
    owner = seeds[0].split("/", 1)[0]
    nodes: dict[str, Mapping[str, Any]] = {}
    edges: dict[str, Edges] = {}
    queue, seen = list(seeds), set(seeds)
    while queue:
        repo = queue.pop(0)
        # one query per repo: all of them in one exceeds what GitHub answers (HTTP 502)
        result = gh_api_graphql(query([repo]), token)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise CIError(f"{repo}: the GraphQL query for the default branch failed")
        node = data.get(f"r_{_alias(repo)}")
        if not node or not node.get("defaultBranchRef"):
            print(f"::warning::{repo}: not readable with this token; skipped", file=sys.stderr)
            continue
        text = (node.get("manifest") or {}).get("text")
        if text is None:
            print(f"::notice::{repo}: no .ci/manifest.toml on the default branch yet; skipped", file=sys.stderr)
            continue
        nodes[repo], edges[repo] = node, edges_of(text, repo)
        for nxt in (*edges[repo].upstream, *edges[repo].downstream):
            if nxt not in seen and nxt.split("/", 1)[0] == owner:
                seen.add(nxt)
                queue.append(nxt)
    return nodes, edges


def fetch(seeds: Sequence[str], token: str | None) -> dict[str, RepoState]:
    """The state of every repo `discover` finds."""
    nodes, edges = discover(seeds, token)
    return {
        repo: repo_state(
            repo,
            node,
            _runs(repo, node["defaultBranchRef"]["name"], token),
            tuple(u for u in edges[repo].upstream if u in nodes),
        )
        for repo, node in nodes.items()
    }


def ordered(upstream: Mapping[str, Sequence[str]]) -> list[str]:
    try:
        return list(TopologicalSorter(upstream).static_order())
    except CycleError as exc:
        raise CIError(f"the manifests' deps form a cycle: {exc.args[1]}") from exc


def red_upstream(states: Mapping[str, RepoState]) -> dict[str, tuple[str, ...]]:
    """Per repo, every transitive upstream whose own CI is red."""
    red: dict[str, tuple[str, ...]] = {}
    for repo in ordered({r: s.upstream for r, s in states.items()}):
        found: dict[str, None] = {}
        for up in states[repo].upstream:
            if states[up].verdict == "red":
                found[up] = None
            found.update(dict.fromkeys(red[up]))
        red[repo] = tuple(found)
    return red


def status(state: RepoState, blockers: Sequence[str]) -> Status:
    names = ", ".join(b.split("/", 1)[1] for b in blockers)
    if state.verdict == "red":
        return Status("❌", "CI failed" + (f", upstream {names} failing too" if blockers else ""), False)
    if blockers:
        return Status("⛔", f"blocked by {names}", False)
    if state.verdict == "green":
        return Status("✅", "CI green", True)
    return Status("❔", f"no completed {CI_WORKFLOW} run on {state.branch}", True)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("<", "&lt;").replace("\n", " ")


def _run_icon(run: Run | None) -> str:
    if run is None:
        return "·"
    if run.conclusion is None:
        return "🟡"
    if run.conclusion in _RED:
        return "❌"
    return "✅" if run.conclusion == "success" else "⚪"


def _user(login: str) -> str:
    if not login:
        return "?"
    if login.endswith("[bot]"):
        return f"[@{login}](https://github.com/apps/{login.removesuffix('[bot]')})"
    return f"[@{login}](https://github.com/{login})"


def _pusher(c: Commit) -> str:
    return c.run.pusher if c.run and c.run.pusher else c.author


def render_summary(
    states: Mapping[str, RepoState],
    now: datetime,
    exclude: Collection[str] = (),
    since: date | None = None,
    until: date | None = None,
) -> str:
    """The dashboard; its table of direct pushes covers `since` to `until`, both whole days (default: the
    last 14 days), and leaves out the people in `exclude`."""
    blockers = red_upstream(states)
    order = ordered({r: s.upstream for r, s in states.items()})
    lines = [
        "# Default branches",
        "",
        f"{now:%Y-%m-%d %H:%M} UTC, the last completed `{CI_WORKFLOW}` run of each default branch.",
        "",
        "| | Repo | Branch | Last completed run | Upstream | Status |",
        "|---|---|---|---|---|---|",
    ]
    for repo in order:
        s = states[repo]
        st = status(s, blockers[repo])
        last = s.last_completed
        run = f"[`{last.sha[:8]}`]({last.url}) {last.conclusion}" if last else ""
        if s.runs and s.runs[0].conclusion is None:
            run += f", 🟡 [`{s.runs[0].sha[:8]}`]({s.runs[0].url}) running"
        ups = ", ".join(states[u].name for u in s.upstream)
        lines.append(
            f"| {st.icon} | [{s.name}](https://github.com/{repo}) | `{s.branch}` | {run} | {ups} | {st.text} |"
        )
    lines += [
        "",
        "✅ green · ❌ CI failed · ⛔ blocked by a failing upstream · 🟡 running · ⚪ cancelled or skipped"
        " · · no run of its own, part of a later push · ❗ pushed without a pull request",
    ]
    start = datetime.combine(since, time(), UTC) if since else now - _DIRECT_PUSH_WINDOW
    end = datetime.combine(until + timedelta(days=1), time(), UTC) if until else datetime.max.replace(tzinfo=UTC)
    pushes = sorted(
        (
            (s, c)
            for s in states.values()
            for c in s.commits
            if c.pr is None and start <= c.date < end and _pusher(c) not in exclude
        ),
        key=lambda sc: sc[1].date,
        reverse=True,
    )
    lines += ["", f"## ❗ Direct pushes from {start:%Y-%m-%d} to {until or now:%Y-%m-%d}", ""]
    if exclude:
        lines += ["Leaving out " + ", ".join(_user(login) for login in sorted(exclude)) + ".", ""]
    if pushes:
        lines += ["| Date | Commit | CI | Person | Title |", "|---|---|---|---|---|"]
        for s, c in pushes:
            ci = f"[{_run_icon(c.run)}]({c.run.url})" if c.run else _run_icon(None)
            commit = f"{s.name} [`{c.sha[:8]}`]({c.url})"
            lines.append(f"| {c.date:%Y-%m-%d %H:%M} | {commit} | {ci} | {_user(_pusher(c))} | {_cell(c.headline)} |")
    else:
        lines.append("None.")
    for repo in order:
        s = states[repo]
        st = status(s, blockers[repo])
        lines += [
            "",
            f"<details{'' if st.ok else ' open'}><summary>{st.icon} <b>{s.name}</b> · {s.branch}</summary>",
            "",
            "| CI | Commit | Author | Via | Message |",
            "|---|---|---|---|---|",
        ]
        for c in s.commits[:_TIMELINE]:
            if c.pr:
                via = f"[#{c.pr[0]}]({c.pr[1]})"
            else:
                pusher = f" by {_user(c.run.pusher)}" if c.run and c.run.pusher else ""
                via = f"❗ direct push{pusher}"
            ci = f"[{_run_icon(c.run)}]({c.run.url})" if c.run else _run_icon(None)
            lines.append(f"| {ci} | [`{c.sha[:8]}`]({c.url}) | {_user(c.author)} | {via} | {_cell(c.headline)} |")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def failures(states: Mapping[str, RepoState]) -> list[str]:
    """One line per repo whose default branch is red or blocked, upstream first."""
    blockers = red_upstream(states)
    out = []
    for repo in ordered({r: s.upstream for r, s in states.items()}):
        st = status(states[repo], blockers[repo])
        if not st.ok:
            out.append(f"{states[repo].name}: {st.text}")
    return out


@click.command()
@click.option("--exclude", multiple=True, metavar="LOGIN", help="Leave this person's direct pushes out of their table.")
@click.option(
    "--since",
    type=click.DateTime(["%Y-%m-%d"]),
    help="First day of the direct pushes, UTC. Default: 14 days ago. Only each repo's last 100 commits are read.",
)
@click.option("--until", type=click.DateTime(["%Y-%m-%d"]), help="Last day of the direct pushes, UTC. Default: today.")
def main(exclude: tuple[str, ...], since: datetime | None, until: datetime | None) -> None:
    """Write the dashboard of the rolled-out repos' default branches to $GITHUB_STEP_SUMMARY.

    Fails when a default branch is red or blocked by a red upstream.
    """
    if since and until and since > until:
        raise click.BadParameter("must not be after --until", param_hint="--since")
    try:
        states = fetch(SEEDS, select_token())
        summary = render_summary(
            states,
            datetime.now(UTC),
            frozenset(exclude),
            since.date() if since else None,
            until.date() if until else None,
        )
    except CIError as exc:
        raise click.ClickException(str(exc)) from exc
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text(summary, encoding="utf-8")
        click.echo(f"Rolled-out repos: {' '.join(sorted(states))}")
    else:
        click.echo(summary)
    failed = failures(states)
    for line in failed:
        name, _, text = line.partition(": ")
        print(f"::error title={name}::{text}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
