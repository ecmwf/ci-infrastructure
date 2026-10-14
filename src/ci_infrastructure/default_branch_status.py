#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Dashboard of the default branches of the rolled-out repos: their CI, who broke it, who pushed without a PR.

`collect` writes the dashboard to $GITHUB_STEP_SUMMARY and one verdict per repo to $GITHUB_OUTPUT;
`generate-workflow` writes the workflow whose jobs need each other as the manifests' `[[deps]]` do.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Final, Literal

import click
import yaml

from . import manifest
from ._errors import CIError
from ._github_api import IN_PROGRESS_STATUSES, ManifestSchemaError, _alias, gh_api_graphql, gh_api_rest, select_token

#: Repos rolled out, or being rolled out; one without .ci/manifest.toml on its default branch is skipped.
REPOS: Final = (
    "ecmwf/ecbuild",
    "ecmwf/stack-dependencies",
    "ecmwf/eckit",
    "ecmwf/eccodes",
    "ecmwf/ecflow",
    "ecmwf/fckit",
    "ecmwf/metkit",
    "ecmwf/fdb",
    "ecmwf/mir",
    "ecmwf/atlas",
)
CI_WORKFLOW: Final = "ci.yml"
WORKFLOW_PATH: Final = ".github/workflows/default-branch-status.yml"
_HISTORY: Final = 100  # commits fetched to walk the first-parent chain
_TIMELINE: Final = 15  # first-parent commits shown per repo

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
            "      oid url messageHeadline\n"
            "      parents(first: 1) { nodes { oid } }\n"
            "      author { name user { login } }\n"
            "      associatedPullRequests(first: 10) { nodes { number url merged baseRefName } }\n"
            "    } }\n"
            "  } } }\n"
            "}"
        )
    return "{\n" + "\n".join(parts) + "\n}"


def upstream_of(manifest_text: str, repo: str, repos: Sequence[str]) -> tuple[str, ...]:
    """The repos among `repos` that `repo`'s manifest depends on."""
    try:
        deps = manifest.validate(tomllib.loads(manifest_text)).deps
    except (tomllib.TOMLDecodeError, ManifestSchemaError) as exc:
        print(f"::warning::{repo}: unreadable manifest, no upstream edges: {exc}", file=sys.stderr)
        return ()
    return tuple(r for r in repos if r != repo and any(d.repo == r for d in deps))


def first_parent(nodes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The first-parent chain from the head; the commits a merged PR brought along are skipped."""
    by_oid = {n["oid"]: n for n in nodes}
    chain: list[Mapping[str, Any]] = []
    node = nodes[0] if nodes else None
    while node is not None and len(chain) < _TIMELINE:
        chain.append(node)
        parents = node["parents"]["nodes"]
        node = by_oid.get(parents[0]["oid"]) if parents else None
    return chain


def _merged_pr(node: Mapping[str, Any], branch: str) -> tuple[int, str] | None:
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
            pusher=(r.get("triggering_actor") or r.get("actor") or {}).get("login", ""),
        )
        for r in data.get("workflow_runs", [])
        if r["event"] != "pull_request"
    )


def repo_state(repo: str, data: Mapping[str, Any], runs: tuple[Run, ...], repos: Sequence[str]) -> RepoState:
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
        )
        for n in first_parent(ref["target"]["history"]["nodes"])
    )
    text = _manifest(data)
    upstream = upstream_of(text, repo, repos) if text is not None else ()
    return RepoState(repo, branch, upstream, commits, runs)


def _manifest(data: Mapping[str, Any]) -> str | None:
    return (data.get("manifest") or {}).get("text")


def fetch(repos: Sequence[str], token: str | None) -> dict[str, RepoState]:
    """The repos with a manifest on their default branch; the others are skipped."""
    nodes = {}
    for repo in repos:
        # one query per repo: all of them in one exceeds what GitHub answers (HTTP 502)
        result = gh_api_graphql(_query([repo]), token)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            raise CIError(f"{repo}: the GraphQL query for the default branch failed")
        node = data.get(f"r_{_alias(repo)}")
        if not node or not node.get("defaultBranchRef"):
            raise CIError(f"{repo}: not readable with this token")
        if _manifest(node) is None:
            print(f"::notice::{repo}: no .ci/manifest.toml on the default branch yet; skipped", file=sys.stderr)
            continue
        nodes[repo] = node
    return {
        repo: repo_state(repo, node, _runs(repo, node["defaultBranchRef"]["name"], token), list(nodes))
        for repo, node in nodes.items()
    }


def job_graph(states: Mapping[str, RepoState], repos: Sequence[str] = REPOS) -> dict[str, tuple[str, ...]]:
    """Every repo's upstream repos; a skipped repo has none."""
    return {r: states[r].upstream if r in states else () for r in repos}


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


def render_summary(states: Mapping[str, RepoState], now: datetime) -> str:
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
        for c in s.commits:
            if c.pr:
                via = f"[#{c.pr[0]}]({c.pr[1]})"
            else:
                pusher = f" by {_user(c.run.pusher)}" if c.run and c.run.pusher else ""
                via = f"❗ direct push{pusher}"
            ci = f"[{_run_icon(c.run)}]({c.run.url})" if c.run else _run_icon(None)
            lines.append(f"| {ci} | [`{c.sha[:8]}`]({c.url}) | {_user(c.author)} | {via} | {_cell(c.headline)} |")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def verdicts(states: Mapping[str, RepoState], repos: Sequence[str] = REPOS) -> dict[str, dict[str, Any]]:
    blockers = red_upstream(states)
    out = {}
    for repo in repos:
        name = repo.split("/", 1)[1]
        if repo not in states:
            out[name] = {"ok": True, "message": f"⚪ {name}: no .ci/manifest.toml on the default branch yet, skipped"}
            continue
        st = status(states[repo], blockers[repo])
        out[name] = {"ok": st.ok, "message": f"{st.icon} {name}: {st.text}"}
    return out


_CHECKOUT: Final = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6"
_APP_TOKEN: Final = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1 # v3"


def render_workflow(upstream: Mapping[str, Sequence[str]]) -> str:
    head = f"""\
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Generated by `ci-infrastructure-default-branch-status generate-workflow`; do not edit.
# A repo's job needs its upstream repos' jobs, and runs even when one failed, to fail with the reason.
name: Default-branch status

on:
  schedule:
    - cron: '17 */6 * * *'
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: default-branch-status
  cancel-in-progress: true

jobs:
  collect:
    runs-on: ubuntu-slim
    outputs:
      verdicts: ${{{{ steps.collect.outputs.verdicts }}}}
    steps:
      - uses: {_CHECKOUT}
        with:
          persist-credentials: false

      - id: token
        uses: {_APP_TOKEN}
        with:
          client-id: ${{{{ secrets.CI_PERMISSIONS_APP_CLIENT_ID }}}}
          private-key: ${{{{ secrets.CI_PERMISSIONS_APP_PRIVATE_KEY }}}}
          owner: ${{{{ github.repository_owner }}}}
          permission-contents: read
          permission-actions: read
          permission-pull-requests: read

      - uses: ./actions/ensure-infrastructure-present

      - id: collect
        shell: bash
        env:
          GH_TOKEN: ${{{{ steps.token.outputs.token }}}}
        run: |
          "$CI_INFRASTRUCTURE_PYTHON" -I -u -m ci_infrastructure.default_branch_status \\
            collect --workflow {WORKFLOW_PATH}
"""
    jobs = []
    for repo in ordered(upstream):
        name = repo.split("/", 1)[1]
        needs = ", ".join(["collect", *(u.split("/", 1)[1] for u in upstream[repo])])
        verdict = f"fromJSON(needs.collect.outputs.verdicts)['{name}']"
        jobs.append(f"""
  {name}:
    needs: [{needs}]
    if: ${{{{ !cancelled() && needs.collect.result == 'success' }}}}
    runs-on: ubuntu-slim
    steps:
      - shell: bash
        env:
          OK: ${{{{ {verdict}.ok }}}}
          MESSAGE: ${{{{ {verdict}.message }}}}
        run: |
          echo "$MESSAGE"
          if [ "$OK" != true ]; then echo "::error title={name}::$MESSAGE"; exit 1; fi
""")
    return head + "".join(jobs)


def _upstream_from_workflow(text: str) -> dict[str, tuple[str, ...]]:
    jobs = yaml.safe_load(text)["jobs"]
    by_name = {r.split("/", 1)[1]: r for r in REPOS}
    return {
        by_name[job]: tuple(by_name[n] for n in body["needs"] if n != "collect")
        for job, body in jobs.items()
        if job != "collect"
    }


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


@click.group()
def main() -> None:
    """Dashboard of the rolled-out repos' default branches."""


@main.command()
@click.option(
    "--workflow",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The generated workflow; a warning if its job graph no longer matches the manifests.",
)
def collect(workflow: Path | None) -> None:
    """Write the dashboard to $GITHUB_STEP_SUMMARY and the per-repo verdicts to $GITHUB_OUTPUT."""
    try:
        states = fetch(REPOS, select_token())
        summary = render_summary(states, datetime.now(UTC))
    except CIError as exc:
        raise click.ClickException(str(exc)) from exc
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text(summary, encoding="utf-8")
    else:
        click.echo(summary)
    _write_output("verdicts", json.dumps(verdicts(states), ensure_ascii=False))
    if workflow is not None:
        if _upstream_from_workflow(workflow.read_text()) != job_graph(states):
            print(
                f"::warning::{workflow}'s job graph differs from the manifests;"
                " run `ci-infrastructure-default-branch-status generate-workflow`",
                file=sys.stderr,
            )


@main.command("generate-workflow")
@click.option(
    "--output", type=click.Path(dir_okay=False, path_type=Path), default=Path(WORKFLOW_PATH), show_default=True
)
def generate_workflow(output: Path) -> None:
    """Write the workflow, with one job per repo that needs its upstream repos' jobs."""
    try:
        states = fetch(REPOS, select_token())
        text = render_workflow(job_graph(states))
    except CIError as exc:
        raise click.ClickException(str(exc)) from exc
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
