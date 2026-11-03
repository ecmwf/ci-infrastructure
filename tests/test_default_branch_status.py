# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import pytest

from ci_infrastructure import default_branch_status as dbs
from ci_infrastructure._errors import CIError
from ci_infrastructure._github_api import _alias

NOW: Final = datetime(2026, 10, 14, 6, 17, tzinfo=UTC)


def _run(sha: str, conclusion: str | None = "success", pusher: str = "dev") -> dbs.Run:
    return dbs.Run(sha=sha, url=f"https://ci/{sha}", conclusion=conclusion, pusher=pusher)


def _state(
    repo: str, *runs: dbs.Run, upstream: tuple[str, ...] = (), commits: tuple[dbs.Commit, ...] = ()
) -> dbs.RepoState:
    return dbs.RepoState(repo=repo, branch="develop", upstream=upstream, commits=commits, runs=runs)


def _node(oid: str, parent: str | None, prs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "oid": oid,
        "url": f"https://commit/{oid}",
        "messageHeadline": f"commit {oid} | piped",
        "committedDate": "2026-10-14T06:00:00Z",
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
    assert dbs._merged_pr(_node("x", None, [open_pr, merged]), "develop") == (3, "u3", "develop")


def test_the_merge_commit_of_a_pr_into_another_branch_counts() -> None:
    # fdb#380: merged into rust-crates, which develop then fast-forwarded to.
    pr = {"number": 380, "url": "u", "merged": True, "baseRefName": "rust-crates", "mergeCommit": {"oid": "x"}}
    assert dbs._merged_pr(_node("x", None, [pr]), "develop") == (380, "u", "rust-crates")
    assert dbs._merged_pr(_node("y", None, [pr]), "develop") is None


def test_a_pr_merged_by_hand_counts_through_the_merged_parent() -> None:
    # ecflow#487: `git merge --no-ff` pushed; GitHub links the PR to the head, not to the merge.
    node = _node("m", "a")
    pr = {"number": 487, "url": "u", "merged": True, "baseRefName": "develop", "mergeCommit": {"oid": "h~1"}}
    node["parents"]["nodes"].append({"oid": "h", "associatedPullRequests": {"nodes": [pr]}})
    assert dbs._merged_pr(node, "develop") == (487, "u", "develop")


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


def test_failures_name_every_red_or_blocked_repo_upstream_first() -> None:
    assert dbs.failures(_graph({"o/eckit"})) == [
        "eckit: CI failed",
        "fckit: blocked by eckit",
        "atlas: blocked by eckit",
    ]
    assert dbs.failures(_graph(set())) == []


def test_own_failure_names_the_failing_upstream_too() -> None:
    states = _graph({"o/ecbuild", "o/fckit"})
    blockers = dbs.red_upstream(states)
    assert blockers["o/atlas"] == ("o/ecbuild", "o/fckit")
    assert dbs.status(states["o/fckit"], blockers["o/fckit"]).text == "CI failed, upstream ecbuild failing too"


def test_a_cycle_is_an_error() -> None:
    with pytest.raises(CIError, match="cycle"):
        dbs.ordered({"o/a": ("o/b",), "o/b": ("o/a",)})


_ATLAS: Final = """
[package]
visibility = "private"
name = "atlas"
repo = "o/atlas"
compiler-inputs = []

[[deps]]
repo = "o/eckit"
package = "eckit"
ref = "develop"
compiler-inputs = []

[packages.atlas-rust]
compiler-inputs = []

[[packages.atlas-rust.deps]]
repo = "o/fckit"
package = "fckit"
ref = "develop"
compiler-inputs = []

[[trigger-downstream]]
repo = "o/mir"
ref = "develop"
"""


def test_edges_are_the_deps_of_every_package_and_the_downstream_triggers() -> None:
    assert dbs.edges_of(_ATLAS, "o/atlas") == dbs.Edges(upstream=("o/eckit", "o/fckit"), downstream=("o/mir",))
    assert dbs.edges_of("not = [toml", "o/atlas") == dbs.Edges((), ())


def _repo_node(manifest: str | None) -> dict[str, Any]:
    return {
        "manifest": {"text": manifest} if manifest is not None else None,
        "defaultBranchRef": {"name": "develop", "target": {"history": {"nodes": []}}},
    }


def test_discovery_follows_deps_and_triggers_but_stays_in_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """ecbuild names eckit downstream; eckit needs stack-deps; atlas is found from eckit; mir has no manifest yet."""

    def pkg(name: str, body: str = "") -> str:
        return f'[package]\nname = "{name}"\nrepo = "o/{name}"\nvisibility = "private"\ncompiler-inputs = []\n{body}'

    def trigger(repo: str) -> str:
        return f'[[trigger-downstream]]\nrepo = "{repo}"\nref = "develop"\n'

    def dep(repo: str) -> str:
        return f'[[deps]]\nrepo = "{repo}"\npackage = "x"\nref = "develop"\ncompiler-inputs = []\n'

    manifests = {
        "o/ecbuild": pkg("ecbuild", trigger("o/eckit")),
        "o/eckit": pkg("eckit", dep("o/ecbuild") + dep("o/stack") + trigger("o/atlas") + trigger("elsewhere/x")),
        "o/stack": pkg("stack"),
        "o/atlas": pkg("atlas", dep("o/eckit") + trigger("o/mir")),
        "o/mir": None,
    }
    asked: list[str] = []

    def graphql(query: str, token: str | None) -> dict[str, Any]:
        repo = next(r for r in manifests if f'name: "{r.split("/")[1]}"' in query)
        asked.append(repo)
        return {"data": {f"r_{_alias(repo)}": _repo_node(manifests[repo])}}

    monkeypatch.setattr(dbs, "gh_api_graphql", graphql)
    monkeypatch.setattr(dbs, "_runs", lambda repo, branch, token: ())

    states = dbs.fetch(("o/ecbuild",), None)

    assert sorted(states) == ["o/atlas", "o/ecbuild", "o/eckit", "o/stack"]
    assert sorted(asked) == ["o/atlas", "o/ecbuild", "o/eckit", "o/mir", "o/stack"]
    assert states["o/eckit"].upstream == ("o/ecbuild", "o/stack")
    assert states["o/atlas"].upstream == ("o/eckit",)


def test_summary_flags_direct_pushes_and_opens_failing_repos() -> None:
    commits = (
        dbs.Commit("b" * 40, "https://c/b", "hotfix | merge", "alice", None, _run("b" * 40, "failure", "bob"), NOW),
        dbs.Commit("a" * 40, "https://c/a", "feature", "carol", (7, "https://pr/7", "develop"), None, NOW),
    )
    states = {
        "o/eckit": _state("o/eckit", _run("b" * 40, "failure", "bob"), commits=commits),
        "o/atlas": _state("o/atlas", _run("z", "success"), upstream=("o/eckit",)),
    }
    out = dbs.render_summary(states, NOW)
    assert "2026-10-14 06:17 UTC" in out
    assert "| ⛔ | [atlas](https://github.com/o/atlas) | `develop` |" in out
    assert "❗ direct push by [@bob](https://github.com/bob) | hotfix \\| merge |" in out
    assert "| · | [`aaaaaaaa`](https://c/a) | [@carol](https://github.com/carol) | [#7](https://pr/7) |" in out
    assert "<details open><summary>❌ <b>eckit</b>" in out
    assert "<details open><summary>⛔ <b>atlas</b>" in out


def test_direct_pushes_of_the_last_14_days_are_listed_newest_first() -> None:
    older = dbs.Commit("d" * 40, "https://c/d", "fix", "erin", None, None, NOW - timedelta(days=2))
    newer = dbs.Commit("b" * 40, "https://c/b", "hotfix", "alice", None, _run("b" * 40, "failure", "bob"), NOW)
    old = dbs.Commit("c" * 40, "https://c/c", "old push", "dave", None, None, NOW - timedelta(days=15))
    merged = dbs.Commit("a" * 40, "https://c/a", "feature", "carol", (7, "https://pr/7", "develop"), None, NOW)
    states = {
        "o/eckit": _state("o/eckit", commits=(newer, merged, old)),
        "o/atlas": _state("o/atlas", commits=(older,)),
    }
    out = dbs.render_summary(states, NOW)
    section = out[out.index("## ❗ Direct pushes from 2026-09-30 to 2026-10-14") : out.index("## ⚠️")]
    ci = f"[❌](https://ci/{'b' * 40})"
    assert (
        f"| 2026-10-14 06:17 | eckit [`bbbbbbbb`](https://c/b) | · | {ci} | [@bob](https://github.com/bob) | hotfix |"
        in section
    )
    assert (
        "| 2026-10-12 06:17 | atlas [`dddddddd`](https://c/d) | · | · | [@erin](https://github.com/erin) | fix |"
        in section
    )
    assert section.index("bbbbbbbb") < section.index("dddddddd")
    assert "cccccccc" not in section and "aaaaaaaa" not in section
    assert "None." in dbs.render_summary({"o/eckit": _state("o/eckit", commits=(merged,))}, NOW)


def test_direct_pushes_between_two_days_include_both_days() -> None:
    def push(sha: str, when: datetime) -> dbs.Commit:
        return dbs.Commit(sha * 40, f"https://c/{sha}", "fix", "erin", None, None, when)

    day = datetime(2026, 9, 1, tzinfo=UTC)
    commits = (
        push("e", day + timedelta(days=3)),  # the day after `until`
        push("d", day + timedelta(days=2, hours=23, minutes=59)),
        push("b", day),
        push("a", day - timedelta(minutes=1)),
    )
    out = dbs.render_summary(
        {"o/eckit": _state("o/eckit", commits=commits)}, NOW, since=date(2026, 9, 1), until=date(2026, 9, 3)
    )
    section = out[out.index("## ❗ Direct pushes from 2026-09-01 to 2026-09-03") : out.index("<details")]
    assert "dddddddd" in section and "bbbbbbbb" in section
    assert "eeeeeeee" not in section and "aaaaaaaa" not in section


def test_the_excluded_peoples_direct_pushes_stay_out_of_the_table() -> None:
    bot = dbs.Commit("b" * 40, "https://c/b", "regen", "x", None, _run("b" * 40, "success", "regen[bot]"), NOW)
    human = dbs.Commit("d" * 40, "https://c/d", "fix", "erin", None, None, NOW)
    out = dbs.render_summary({"o/eckit": _state("o/eckit", commits=(bot, human))}, NOW, {"regen[bot]"})
    section = out[out.index("## ❗ Direct pushes") : out.index("<details")]
    assert "Leaving out [@regen[bot]](https://github.com/apps/regen)." in section
    assert "dddddddd" in section and "bbbbbbbb" not in section
    assert "bbbbbbbb" in out[out.index("<details") :]


def test_bots_link_to_their_app() -> None:
    assert dbs._user("regen[bot]") == "[@regen[bot]](https://github.com/apps/regen)"


def test_pusher_is_the_first_attempts_actor_not_the_rerunner(monkeypatch: pytest.MonkeyPatch) -> None:
    run = {
        "head_sha": "abc",
        "html_url": "https://ci/abc",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "actor": {"login": "pusher"},
        "triggering_actor": {"login": "rerunner"},
    }
    monkeypatch.setattr(dbs, "gh_api_rest", lambda path, token: {"workflow_runs": [run]})
    assert [r.pusher for r in dbs._runs("ecmwf/x", "develop", None)] == ["pusher"]


def _merge(sha: str, when: datetime, *bypassed: str, merger: str = "mo") -> dbs.Commit:
    pr = (int(sha, 16), f"https://pr/{sha}", "develop")
    merge = dbs.Merge(merger, f"PR {sha} | piped", bypassed)
    return dbs.Commit(sha * 40, f"https://c/{sha}", "Merge", "x", pr, _run(sha * 40), when, before="❌", merge=merge)


def test_prs_merged_before_their_ci_was_green_are_listed_newest_first() -> None:
    commits = (
        _merge("a", NOW, "ci-result pending", "downstream/runner missing"),
        _merge("b", NOW - timedelta(days=1)),
        _merge("c", NOW - timedelta(days=2), "ci-result failure", merger="regen[bot]"),
        _merge("d", NOW - timedelta(days=3), "ci-result failure"),
        _merge("e", NOW - timedelta(days=15), "ci-result failure"),
    )
    state = replace(_state("o/eckit", commits=commits), rules_since=NOW - timedelta(days=20))
    out = dbs.render_summary({"o/eckit": state}, NOW, {"regen[bot]"})
    section = out[
        out.index("## ⚠️ Merged before the required CI was green from 2026-09-30 to 2026-10-14") : out.index("<details")
    ]
    assert "Required statuses last changed (UTC): eckit 2026-09-24 06:17." in section
    assert "| Date | Commit | CI before | CI | Person | Title | Not green |" in section
    assert (
        f"| 2026-10-14 06:17 | eckit [#10](https://pr/a) | ❌ | [✅](https://ci/{'a' * 40})"
        " | [@mo](https://github.com/mo) | PR a \\| piped | ci-result pending, downstream/runner missing |" in section
    )
    assert section.index("#10") < section.index("#13")
    assert "#11" not in section and "#12" not in section and "#14" not in section
    assert "None." in dbs.render_summary({"o/eckit": _state("o/eckit", commits=commits[1:2])}, NOW)


def test_a_rule_counts_by_its_state_when_the_pr_was_merged() -> None:
    t = NOW
    statuses = [
        ("ci-result", "success", t + timedelta(minutes=1)),
        ("ci-result", "pending", t - timedelta(minutes=9)),
        ("downstream/runner", "success", t - timedelta(minutes=5)),
        ("ci-result/hpc-atos", "failure", t - timedelta(minutes=1)),
    ]
    assert dbs.status_at(statuses, "ci-result", t) == "pending"
    assert dbs.status_at(statuses, "ci-result", t + timedelta(minutes=1)) == "success"
    checks = ("ci-result", "downstream/runner", "ci-result/hpc-atos", "downstream/hpc-atos")
    assert dbs.bypassed(statuses, t, checks) == (
        "ci-result pending",
        "ci-result/hpc-atos failure",
        "downstream/hpc-atos missing",
    )
    assert dbs.bypassed(statuses, t, ("downstream/runner",)) == ()


def test_ci_before_is_the_ci_result_of_what_landed_when_it_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    merged = NOW - timedelta(hours=1)
    pr = dbs.Commit("m" * 40, "u", "Merge", "x", (7, "https://pr/7", "develop"), None, merged)
    run = replace(_run("p" * 40), created=NOW + timedelta(minutes=2))
    push = dbs.Commit("p" * 40, "u", "hotfix", "x", None, run, NOW, head="h" * 40)
    old = dbs.Commit("o" * 40, "u", "old", "x", None, None, NOW - timedelta(days=30), head="o" * 40)
    statuses = {
        "h" * 40: [
            ("ci-result", "success", NOW + timedelta(minutes=1)),
            ("ci-result", "failure", NOW + timedelta(minutes=3)),
        ],
        "head7": [("ci-result", "failure", merged - timedelta(minutes=1)), ("downstream/runner", "success", merged)],
    }
    pr_node = {
        "title": "Feature",
        "headRefOid": "head7",
        "mergedBy": {"login": "mo"},
        "mergedAt": merged.isoformat(),
    }
    rules = [
        {
            "type": "required_status_checks",
            "ruleset_id": 2,
            "parameters": {"required_status_checks": [{"context": "ci-result"}]},
        },
        {"type": "pull_request", "ruleset_id": 1, "parameters": {"required_approving_review_count": 1}},
        {"type": "deletion", "ruleset_id": 1, "parameters": None},
    ]
    rules_since = merged - timedelta(hours=1)
    rulesets = [
        {"id": 1, "updated_at": (merged - timedelta(days=5)).isoformat()},
        {"id": 2, "updated_at": rules_since.isoformat()},
        {"id": 3, "updated_at": NOW.isoformat()},  # a tag ruleset
    ]
    asked: list[str] = []

    def rest(path: str, token: str | None) -> Any:
        asked.append(path)
        if "/rules/branches/" in path:
            return rules
        if path.endswith("/rulesets?per_page=100"):
            return rulesets
        return [{"context": c, "state": st, "created_at": d.isoformat()} for c, st, d in statuses[path.split("/")[4]]]

    monkeypatch.setattr(dbs, "gh_api_rest", rest)
    monkeypatch.setattr(dbs, "gh_api_graphql", lambda q, t: {"data": {"repository": {"p7": pr_node}}})
    state = dbs.with_ci_before(
        _state("o/eckit", commits=(push, pr, old)), NOW - timedelta(days=14), dbs.window(NOW)[1], None
    )
    got = {c.sha[0]: c for c in state.commits}
    assert got["p"].before == "✅" and got["p"].merge is None
    assert got["m"].before == "❌"
    assert got["m"].merge == dbs.Merge("mo", "Feature", ("ci-result failure",))
    assert got["o"].before == "·"
    assert state.rules_since == rules_since

    rulesets[1]["updated_at"] = (merged + timedelta(minutes=1)).isoformat()
    state = dbs.with_ci_before(_state("o/eckit", commits=(pr,)), NOW - timedelta(days=14), dbs.window(NOW)[1], None)
    merge = state.commits[0].merge
    assert merge is not None and merge.bypassed == () and state.commits[0].before == "❌"
    assert not any("o" * 40 in path for path in asked)


def test_a_pr_named_by_several_commits_is_listed_once() -> None:
    # eccodes#564: GitHub links the PR to every first-parent commit it brought along.
    commits = (
        _merge("a", NOW, "ci-result failure"),
        replace(_merge("a", NOW - timedelta(hours=1), "ci-result failure"), sha="f" * 40),
    )
    out = dbs.render_summary({"o/eccodes": _state("o/eccodes", commits=commits)}, NOW)
    assert out.count("eccodes [#10]") == 1
    assert "| 2026-10-14 06:17 | eccodes [#10]" in out


def test_an_apps_login_gets_the_bot_suffix_rest_uses() -> None:
    assert dbs._login({"__typename": "Bot", "login": "regen"}) == "regen[bot]"
    assert dbs._login({"__typename": "User", "login": "mo"}) == "mo"
    assert dbs._login(None) == ""
