# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import pytest

from ci_infrastructure import check_pr_declaration as decl
from ci_infrastructure import sync_branch_prs as sbp
from ci_infrastructure.default_branch_status import Edges

DECLARATION = decl.CANONICAL_DECLARATION.strip("\n")
PREVIEW = "<!-- PREVIEW-URL_BEGIN -->\nhttps://docs/preview\n<!-- PREVIEW-URL_END -->"


def _pr(repo: str, number: int, body: str = "", head: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "body": body,
        "headRepository": {"nameWithOwner": head or repo},
    }


def _graph(
    prs: dict[str, list[dict[str, Any]]], private: frozenset[str] = frozenset()
) -> tuple[dict[str, Any], dict[str, Edges]]:
    upstream = {"o/ecbuild": (), "o/eckit": ("o/ecbuild",), "o/fckit": ("o/eckit",), "o/atlas": ("o/eckit", "o/fckit")}
    nodes = {r: {"isPrivate": r in private, "pullRequests": {"nodes": prs.get(r, [])}} for r in upstream}
    return nodes, {r: Edges(ups, ()) for r, ups in upstream.items()}


def test_upstream_repos_come_first_and_each_pr_once() -> None:
    eckit = [_pr("o/eckit", 9), _pr("o/eckit", 3)]
    prs = sbp.involved(*_graph({"o/atlas": [_pr("o/atlas", 1)], "o/eckit": eckit + [_pr("o/eckit", 3)]}))
    assert [p.url for p in prs] == [
        "https://github.com/o/eckit/pull/3",
        "https://github.com/o/eckit/pull/9",
        "https://github.com/o/atlas/pull/1",
    ]


def test_a_forks_same_named_branch_is_not_involved() -> None:
    prs = sbp.involved(*_graph({"o/eckit": [_pr("o/eckit", 1, head="fork/eckit")], "o/atlas": [_pr("o/atlas", 2)]}))
    assert [p.repo for p in prs] == ["o/atlas"]


def test_the_branch_name_cannot_break_out_of_the_query() -> None:
    query = sbp.query_for('feature/x") { evil }')(["o/eckit"])
    assert 'headRefName: "feature/x\\") { evil }"' in query


def test_the_block_ends_the_description_and_is_replaced_not_repeated() -> None:
    urls = ["https://github.com/o/eckit/pull/3", "https://github.com/o/atlas/pull/1"]
    body = sbp.with_block(f"Change.\n\n{DECLARATION}\n", urls)
    assert body.endswith(
        f"{DECLARATION}\n\n<!-- SYNC-BRANCH_BEGIN -->\nSynchronised rollout:\n\n"
        "- https://github.com/o/eckit/pull/3\n- https://github.com/o/atlas/pull/1\n<!-- SYNC-BRANCH_END -->"
    )
    assert sbp.with_block(body, urls) == body
    longer = sbp.with_block(body, [*urls, "https://github.com/o/fckit/pull/7"])
    assert longer.count(sbp.BEGIN) == 1
    assert longer.endswith("- https://github.com/o/fckit/pull/7\n<!-- SYNC-BRANCH_END -->")


def test_with_fewer_than_two_prs_the_block_goes() -> None:
    plain = f"Change.\n\n{DECLARATION}"
    assert sbp.with_block(plain, ["https://github.com/o/eckit/pull/3"]) == plain
    listed = sbp.with_block(plain, ["https://github.com/o/eckit/pull/3", "https://github.com/o/atlas/pull/1"])
    assert sbp.with_block(listed, ["https://github.com/o/eckit/pull/3"]) == plain


@pytest.mark.parametrize("before", ["", f"\n\n{PREVIEW}"], ids=["alone", "after-preview"])
def test_the_declaration_check_ignores_the_block(before: str) -> None:
    urls = ["https://github.com/o/eckit/pull/3", "https://github.com/o/atlas/pull/1"]
    body = sbp.with_block(f"Change.\n\n{DECLARATION}{before}", urls)
    assert decl.check_body(body).verdict is decl.Verdict.OK


def test_only_changed_descriptions_are_rewritten() -> None:
    urls = ["https://github.com/o/eckit/pull/3", "https://github.com/o/atlas/pull/1"]
    done = sbp.with_block("Change.", urls)
    prs = sbp.involved(*_graph({"o/eckit": [_pr("o/eckit", 3, done)], "o/atlas": [_pr("o/atlas", 1, "Other.")]}))
    assert [(pr.url, body) for pr, body in sbp.updates(prs)] == [(urls[1], sbp.with_block("Other.", urls))]


def test_a_public_pr_lists_only_public_repos() -> None:
    graph = _graph(
        {"o/ecbuild": [_pr("o/ecbuild", 5)], "o/eckit": [_pr("o/eckit", 3)], "o/atlas": [_pr("o/atlas", 1)]},
        private=frozenset({"o/ecbuild"}),
    )
    new = {pr.repo: body for pr, body in sbp.updates(sbp.involved(*graph))}
    assert "o/ecbuild/pull/5" in new["o/ecbuild"]
    assert "o/ecbuild" not in new["o/eckit"]
    assert "o/eckit/pull/3" in new["o/atlas"]
