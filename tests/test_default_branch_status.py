# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _node(oid: str, parent: str | None) -> dict[str, Any]:
    return {
        "oid": oid,
        "url": f"https://commit/{oid}",
        "messageHeadline": f"commit {oid} | piped",
        "message": f"commit {oid} | piped\n\nbody",
        "committedDate": "2026-10-14T06:00:00Z",
        "parents": {"nodes": [{"oid": parent}] if parent else []},
        "author": {"name": "Some Dev", "user": {"login": "dev"}},
    }


def test_first_parent_skips_the_commits_a_merge_brought_along() -> None:
    # m merges PR branch b2 -> b1 into a; history lists them all.
    nodes = [_node("m", "a"), _node("b2", "b1"), _node("b1", "a"), _node("a", None)]
    assert [n["oid"] for n in dbs.first_parent(nodes)] == ["m", "a"]


def test_merge_and_squash_headlines_name_their_pr() -> None:
    assert dbs._merged_pr("Merge pull request #374 from ecmwf/feature/eckit-geo", "o/r") == (
        374,
        "https://github.com/o/r/pull/374",
    )
    assert dbs._merged_pr("Gate Rust geo tests behind the geo feature (#379)", "o/r") == (
        379,
        "https://github.com/o/r/pull/379",
    )
    assert dbs._merged_pr("Merge branch 'release/2.5.0' into develop", "o/r") is None
    assert dbs._merged_pr("fix #12 in the middle", "o/r") is None


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
        return f'[package]\nname = "{name}"\nrepo = "o/{name}"\ncompiler-inputs = []\n{body}'

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
        dbs.Commit("a" * 40, "https://c/a", "feature", "carol", (7, "https://pr/7"), None, NOW),
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
    merged = dbs.Commit("a" * 40, "https://c/a", "feature", "carol", (7, "https://pr/7"), None, NOW)
    states = {
        "o/eckit": _state("o/eckit", commits=(newer, merged, old)),
        "o/atlas": _state("o/atlas", commits=(older,)),
    }
    out = dbs.render_summary(states, NOW)
    section = out[out.index("## ❗ Direct pushes since 2026-09-30") : out.index("<details")]
    ci = f"[❌](https://ci/{'b' * 40})"
    assert (
        f"| 2026-10-14 06:17 | eckit [`bbbbbbbb`](https://c/b) | {ci} | [@bob](https://github.com/bob) | hotfix |"
        in section
    )
    assert (
        "| 2026-10-12 06:17 | atlas [`dddddddd`](https://c/d) | · | [@erin](https://github.com/erin) | fix |" in section
    )
    assert section.index("bbbbbbbb") < section.index("dddddddd")
    assert "cccccccc" not in section and "aaaaaaaa" not in section
    assert "None." in dbs.render_summary({"o/eckit": _state("o/eckit", commits=(merged,))}, NOW)


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
