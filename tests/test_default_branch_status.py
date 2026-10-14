# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ci_infrastructure import default_branch_status as dbs
from ci_infrastructure._errors import CIError

ROOT = Path(__file__).resolve().parents[1]


def _run(sha: str, conclusion: str | None = "success", pusher: str = "dev") -> dbs.Run:
    return dbs.Run(sha=sha, url=f"https://ci/{sha}", conclusion=conclusion, pusher=pusher)


def _state(
    repo: str, *runs: dbs.Run, upstream: tuple[str, ...] = (), commits: tuple[dbs.Commit, ...] = ()
) -> dbs.RepoState:
    return dbs.RepoState(repo=repo, branch="develop", upstream=upstream, commits=commits, runs=runs)


def _node(
    oid: str, parent: str | None, prs: list[dict[str, Any]] | None = None, login: str | None = "dev"
) -> dict[str, Any]:
    return {
        "oid": oid,
        "url": f"https://commit/{oid}",
        "messageHeadline": f"commit {oid} | piped",
        "parents": {"nodes": [{"oid": parent}] if parent else []},
        "author": {"name": "Some Dev", "user": {"login": "dev"}},
        "associatedPullRequests": {"nodes": prs or []},
    }


def test_first_parent_skips_the_commits_a_merge_brought_along() -> None:
    # m merges PR branch b2 -> b1 into a; history lists them all.
    nodes = [_node("m", "a"), _node("b2", "b1"), _node("b1", "a"), _node("a", None)]
    assert [n["oid"] for n in dbs.first_parent(nodes)] == ["m", "a"]


def test_only_a_merged_pr_into_the_branch_counts() -> None:
    open_pr = {"number": 1, "url": "u1", "merged": False, "baseRefName": "develop"}
    other_base = {"number": 2, "url": "u2", "merged": True, "baseRefName": "master"}
    merged = {"number": 3, "url": "u3", "merged": True, "baseRefName": "develop"}
    assert dbs._merged_pr(_node("x", None, [open_pr, other_base]), "develop") is None
    assert dbs._merged_pr(_node("x", None, [open_pr, merged]), "develop") == (3, "u3")


def test_verdict_skips_running_and_cancelled_runs() -> None:
    assert _state("o/a", _run("3", None), _run("2", "cancelled"), _run("1", "failure")).verdict == "red"
    assert _state("o/a", _run("2", "success"), _run("1", "failure")).verdict == "green"
    assert _state("o/a", _run("1", None)).verdict == "unknown"


def _graph(red: set[str]) -> dict[str, dbs.RepoState]:
    upstream = {"o/ecbuild": (), "o/eckit": ("o/ecbuild",), "o/fckit": ("o/eckit",), "o/atlas": ("o/eckit", "o/fckit")}
    return {
        repo: _state(repo, _run(repo, "failure" if repo in red else "success"), upstream=ups)
        for repo, ups in upstream.items()
    }


def test_a_red_upstream_blocks_every_transitive_consumer() -> None:
    states = _graph({"o/eckit"})
    blockers = dbs.red_upstream(states)
    assert blockers == {"o/ecbuild": (), "o/eckit": (), "o/fckit": ("o/eckit",), "o/atlas": ("o/eckit",)}
    assert dbs.status(states["o/atlas"], blockers["o/atlas"]) == dbs.Status("⛔", "blocked by eckit", False)
    assert dbs.status(states["o/eckit"], blockers["o/eckit"]) == dbs.Status("❌", "CI failed", False)
    assert dbs.verdicts(states, list(states))["ecbuild"] == {"ok": True, "message": "✅ ecbuild: CI green"}


def test_a_repo_without_manifest_is_skipped_and_passes() -> None:
    states = _graph(set())
    repos = [*states, "o/mir"]
    assert dbs.verdicts(states, repos)["mir"] == {
        "ok": True,
        "message": "⚪ mir: no .ci/manifest.toml on the default branch yet, skipped",
    }
    assert dbs.job_graph(states, repos)["o/mir"] == ()
    assert dbs.job_graph(states, repos)["o/atlas"] == ("o/eckit", "o/fckit")


def test_own_failure_names_the_failing_upstream_too() -> None:
    states = _graph({"o/ecbuild", "o/fckit"})
    blockers = dbs.red_upstream(states)
    assert blockers["o/atlas"] == ("o/ecbuild", "o/fckit")
    assert dbs.status(states["o/fckit"], blockers["o/fckit"]).text == "CI failed, upstream ecbuild failing too"


def test_a_cycle_is_an_error() -> None:
    with pytest.raises(CIError, match="cycle"):
        dbs.ordered({"o/a": ("o/b",), "o/b": ("o/a",)})


def test_upstream_keeps_only_the_dashboard_repos() -> None:
    text = """
[package]
name = "atlas"
repo = "o/atlas"
compiler-inputs = []

[[deps]]
repo = "o/eckit"
package = "eckit"
ref = "develop"
compiler-inputs = []

[[deps]]
repo = "o/elsewhere"
package = "x"
ref = "main"
compiler-inputs = []
"""
    assert dbs.upstream_of(text, "o/atlas", ("o/eckit", "o/atlas")) == ("o/eckit",)
    assert dbs.upstream_of("not = [toml", "o/atlas", ("o/eckit",)) == ()


def test_summary_flags_direct_pushes_and_opens_failing_repos() -> None:
    commits = (
        dbs.Commit("b" * 40, "https://c/b", "hotfix | merge", "alice", None, _run("b" * 40, "failure", "bob")),
        dbs.Commit("a" * 40, "https://c/a", "feature", "carol", (7, "https://pr/7"), None),
    )
    states = {
        "o/eckit": _state("o/eckit", _run("b" * 40, "failure", "bob"), commits=commits),
        "o/atlas": _state("o/atlas", _run("z", "success"), upstream=("o/eckit",)),
    }
    out = dbs.render_summary(states, datetime(2026, 10, 14, 6, 17, tzinfo=UTC))
    assert "2026-10-14 06:17 UTC" in out
    assert "| ⛔ | [atlas](https://github.com/o/atlas) | `develop` |" in out
    assert "❗ direct push by [@bob](https://github.com/bob) | hotfix \\| merge |" in out
    assert "| · | [`aaaaaaaa`](https://c/a) | [@carol](https://github.com/carol) | [#7](https://pr/7) |" in out
    assert "<details open><summary>❌ <b>eckit</b>" in out
    assert "<details open><summary>⛔ <b>atlas</b>" in out


def test_bots_link_to_their_app() -> None:
    assert dbs._user("regen[bot]") == "[@regen[bot]](https://github.com/apps/regen)"


def test_committed_workflow_is_generated_for_the_dashboard_repos() -> None:
    committed = (ROOT / dbs.WORKFLOW_PATH).read_text()
    upstream = dbs._upstream_from_workflow(committed)
    assert set(upstream) == set(dbs.REPOS)
    assert dbs.render_workflow(upstream) == committed
