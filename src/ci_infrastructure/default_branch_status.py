#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Dashboard of the default branches of the rolled-out repos: their CI, who broke it, who pushed without a PR
or merged one without green CI.

The repos are found from ecbuild along the manifests' `[[deps]]` and `[[trigger-downstream]]`.
Writes the dashboard to $GITHUB_STEP_SUMMARY and fails when a repo's default branch is red or blocked.
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
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
CI_RESULT: Final = "ci-result"  # the commit status a ci.yml run sets on what it built
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
    created: datetime | None = None


@dataclass(frozen=True)
class Merge:
    """How a PR into the default branch was merged."""

    merger: str
    title: str
    bypassed: bool  # merged without its required status checks green


@dataclass(frozen=True)
class Commit:
    sha: str
    url: str
    headline: str
    author: str
    pr: tuple[int, str, str] | None  # (number, url, base) of the merged PR that brought it in
    run: Run | None
    date: datetime
    head: str = ""  # what was merged: the last parent of a merge commit, else the commit itself
    before: str = "·"  # the CI_RESULT of `head` (of the PR's head, for a PR) when it landed, as a _run_icon
    merge: Merge | None = None


@dataclass(frozen=True)
class RepoState:
    repo: str
    branch: str
    upstream: tuple[str, ...]
    commits: tuple[Commit, ...]
    runs: tuple[Run, ...]  # newest first
    bypasses_known: bool = True  # False when its rule insights could not be read

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


_PR_FIELDS: Final = "number url merged baseRefName mergeCommit { oid }"


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
            "      parents(first: 2) { nodes { oid\n"
            f"        associatedPullRequests(first: 10) {{ nodes {{ {_PR_FIELDS} }} }}\n"
            "      } }\n"
            "      author { name user { login } }\n"
            f"      associatedPullRequests(first: 10) {{ nodes {{ {_PR_FIELDS} }} }}\n"
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


def _merged_pr(node: Mapping[str, Any], branch: str) -> tuple[int, str, str] | None:
    """(number, url, base) of the merged PR that brought the commit in, whichever way it was merged.

    Also a PR into another branch that this very commit merged (that branch then fast-forwarded here),
    and a PR into `branch` merged by hand: its head is then a later parent of this merge commit.
    """
    prs = [pr for pr in node["associatedPullRequests"]["nodes"] if pr["merged"]]
    for pr in prs:
        if pr["baseRefName"] == branch or (pr.get("mergeCommit") or {}).get("oid") == node["oid"]:
            return int(pr["number"]), str(pr["url"]), str(pr["baseRefName"])
    for parent in node["parents"]["nodes"][1:]:
        for pr in (parent.get("associatedPullRequests") or {}).get("nodes", []):
            if pr["merged"] and pr["baseRefName"] == branch:
                return int(pr["number"]), str(pr["url"]), branch
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
            created=datetime.fromisoformat(r["created_at"]) if r.get("created_at") else None,
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
            head=n["parents"]["nodes"][-1]["oid"] if len(n["parents"]["nodes"]) > 1 else n["oid"],
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


StatusEntry = tuple[str, str, datetime]  # (context, state, created)


def _statuses(repo: str, sha: str, token: str | None) -> list[StatusEntry]:
    data = gh_api_rest(f"repos/{repo}/commits/{sha}/statuses?per_page=100", token)
    if not isinstance(data, list):
        print(f"::warning::{repo}: could not list the statuses of {sha}", file=sys.stderr)
        return []
    return [(s["context"], s["state"], datetime.fromisoformat(s["created_at"])) for s in data]


def status_at(statuses: Sequence[StatusEntry], context: str, when: datetime) -> str | None:
    """The state `context` had at `when`, None if it was not set yet."""
    earlier = [s for s in statuses if s[0] == context and s[2] <= when]
    return max(earlier, key=lambda s: s[2])[1] if earlier else None


def _icon_of(state: str | None) -> str:
    return {None: "·", "pending": "🟡", "success": "✅"}.get(state, "❌")


_TIME_PERIODS: Final = (("hour", timedelta(hours=1)), ("day", timedelta(days=1)), ("week", timedelta(weeks=1)))


def _ci_bypasses(repo: str, branch: str, start: datetime, now: datetime, token: str | None) -> dict[str, str] | None:
    """Per pushed sha from `start` on, who pushed or merged it without its required status checks green.

    From the rule insights (Administration: read), which reach back one month; None when unreadable.
    """
    period = next((p for p, span in _TIME_PERIODS if now - span <= start), "month")
    if period == "month" and now - start > timedelta(days=31):
        print(f"::warning::{repo}: rule insights reach back one month, not to {start:%Y-%m-%d}", file=sys.stderr)
    suites: list[Mapping[str, Any]] = []
    for page in range(1, 11):
        data = gh_api_rest(
            f"repos/{repo}/rulesets/rule-suites?ref=refs/heads/{branch}&time_period={period}"
            f"&rule_suite_result=bypass&per_page=100&page={page}",
            token,
        )
        if not isinstance(data, list):
            return None
        suites += data
        if len(data) < 100:
            break
    out = {}
    for suite in suites:
        if datetime.fromisoformat(suite["pushed_at"]) < start:
            continue
        detail = gh_api_rest(f"repos/{repo}/rulesets/rule-suites/{suite['id']}", token)
        evaluations = detail.get("rule_evaluations", []) if isinstance(detail, dict) else []
        failed = [e for e in evaluations if e.get("result") == "fail"]
        rules = ", ".join(
            f"{(e.get('rule_source') or {}).get('name')}/{e['rule_type']} ({e.get('enforcement')})" for e in failed
        )
        print(f"{repo} {suite['after_sha'][:8]} bypassed by {suite['actor_name']}: {rules}", file=sys.stderr)
        # a rule in evaluate mode fails without blocking anything
        if any(e["rule_type"] == "required_status_checks" and e.get("enforcement") == "active" for e in failed):
            out[suite["after_sha"]] = suite["actor_name"]
    return out


def _login(actor: Mapping[str, Any] | None) -> str:
    """As REST names it: GraphQL leaves the `[bot]` off an app's login."""
    if not actor:
        return ""
    return f"{actor['login']}[bot]" if actor.get("__typename") == "Bot" else str(actor["login"])


def _pr_query(repo: str, numbers: Collection[int]) -> str:
    owner, name = repo.split("/", 1)
    prs = "\n".join(
        f"p{n}: pullRequest(number: {n}) {{ title headRefOid mergedBy {{ __typename login }} mergedAt }}"
        for n in numbers
    )
    return f'{{ repository(owner: "{owner}", name: "{name}") {{\n{prs}\n}} }}'


def with_ci_before(
    state: RepoState, start: datetime, end: datetime, token: str | None, now: datetime | None = None
) -> RepoState:
    """`state` with what its commits from `start` to `end` had passed before they landed,
    and which PRs among them were merged without their required status checks green."""
    landed = [c for c in state.commits if start <= c.date < end and (c.pr is None or c.pr[2] == state.branch)]
    prs: dict[int, Mapping[str, Any]] = {}
    bypasses: dict[str, str] | None = {}
    if any(c.pr for c in landed):
        result = gh_api_graphql(_pr_query(state.repo, {c.pr[0] for c in landed if c.pr}), token)
        data = result.get("data") if isinstance(result, dict) else None
        repo_data = (data or {}).get("repository") or {}
        prs = {int(k[1:]): v for k, v in repo_data.items() if v}
        bypasses = _ci_bypasses(state.repo, state.branch, start, now or datetime.now(UTC), token)
    commits = {}
    for c in landed:
        if c.pr is None:
            when = c.run.created if c.run and c.run.created else c.date
            commits[c.sha] = replace(
                c, before=_icon_of(status_at(_statuses(state.repo, c.head, token), CI_RESULT, when))
            )
            continue
        pr = prs.get(c.pr[0])
        if pr is None:
            continue
        merged = datetime.fromisoformat(pr["mergedAt"])
        statuses = _statuses(state.repo, pr["headRefOid"], token)
        bypasser = (bypasses or {}).get(c.sha)
        merge = Merge(bypasser or _login(pr["mergedBy"]), pr["title"], bypasser is not None)
        commits[c.sha] = replace(c, before=_icon_of(status_at(statuses, CI_RESULT, merged)), merge=merge)
    return replace(
        state, commits=tuple(commits.get(c.sha, c) for c in state.commits), bypasses_known=bypasses is not None
    )


def window(now: datetime, since: date | None = None, until: date | None = None) -> tuple[datetime, datetime]:
    """`since` to `until`, both whole days; by default the last 14 days."""
    start = datetime.combine(since, time(), UTC) if since else now - _DIRECT_PUSH_WINDOW
    end = datetime.combine(until + timedelta(days=1), time(), UTC) if until else datetime.max.replace(tzinfo=UTC)
    return start, end


def fetch(
    seeds: Sequence[str],
    token: str | None,
    start: datetime | None = None,
    end: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, RepoState]:
    """The state of every repo `discover` finds; with `start` and `end`, also `with_ci_before`."""
    nodes, edges = discover(seeds, token)
    states = {
        repo: repo_state(
            repo,
            node,
            _runs(repo, node["defaultBranchRef"]["name"], token),
            tuple(u for u in edges[repo].upstream if u in nodes),
        )
        for repo, node in nodes.items()
    }
    if start is None or end is None:
        return states
    return {repo: with_ci_before(s, start, end, token, now) for repo, s in states.items()}


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


def _ci(c: Commit) -> str:
    return f"[{_run_icon(c.run)}]({c.run.url})" if c.run else _run_icon(None)


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
        " · · no run of its own, part of a later push · ❗ pushed without a pull request"
        " · ⚠️ merged without green CI",
    ]
    start, end = window(now, since, until)
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
    leaving_out = ["Leaving out " + ", ".join(_user(login) for login in sorted(exclude)) + ".", ""] if exclude else []
    lines += leaving_out
    if pushes:
        lines += ["| Date | Commit | CI before | CI | Person | Title |", "|---|---|---|---|---|---|"]
        for s, c in pushes:
            commit = f"{s.name} [`{c.sha[:8]}`]({c.url})"
            lines.append(
                f"| {c.date:%Y-%m-%d %H:%M} | {commit} | {c.before} | {_ci(c)} | {_user(_pusher(c))}"
                f" | {_cell(c.headline)} |"
            )
    else:
        lines.append("None.")
    by_pr = {
        (s.repo, c.pr[0]): (s, c)  # several commits can name one PR; the newest stays
        for s in states.values()
        for c in reversed(s.commits)
        if c.pr and c.merge and c.merge.bypassed and start <= c.date < end and c.merge.merger not in exclude
    }
    merges = sorted(by_pr.values(), key=lambda sc: sc[1].date, reverse=True)
    unknown = [states[r].name for r in order if not states[r].bypasses_known]
    lines += ["", f"## ⚠️ Merged without green CI from {start:%Y-%m-%d} to {until or now:%Y-%m-%d}", ""]
    lines += [
        "Pull requests merged while the status checks that the default branch's rules require were not all green,"
        f" as GitHub's rule insights record it. CI before is their head's `{CI_RESULT}` then.",
        "",
        *leaving_out,
        *([f"Could not read the rule insights of {', '.join(unknown)}.", ""] if unknown else []),
    ]
    if merges:
        lines += ["| Date | Commit | CI before | CI | Person | Title |", "|---|---|---|---|---|---|"]
        for s, c in merges:
            assert c.pr and c.merge
            commit = f"{s.name} [#{c.pr[0]}]({c.pr[1]})"
            lines.append(
                f"| {c.date:%Y-%m-%d %H:%M} | {commit} | {c.before} | {_ci(c)} | {_user(c.merge.merger)}"
                f" | {_cell(c.merge.title)} |"
            )
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
                via = f"[#{c.pr[0]}]({c.pr[1]})" + (f" into `{c.pr[2]}`" if c.pr[2] != s.branch else "")
            else:
                pusher = f" by {_user(c.run.pusher)}" if c.run and c.run.pusher else ""
                via = f"❗ direct push{pusher}"
            lines.append(f"| {_ci(c)} | [`{c.sha[:8]}`]({c.url}) | {_user(c.author)} | {via} | {_cell(c.headline)} |")
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
        now = datetime.now(UTC)
        days = (since.date() if since else None, until.date() if until else None)
        states = fetch(SEEDS, select_token(), *window(now, *days), now)
        summary = render_summary(states, now, frozenset(exclude), *days)
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
