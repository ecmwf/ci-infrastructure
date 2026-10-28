# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from ci_infrastructure import wait_for_deps


def _dep(name: str, cached: bool = False) -> dict[str, Any]:
    return {"repo": "o/eckit", "sha": "a" * 40, "artifact-name": name, "cached": cached}


def _matrix(*legs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"include": [{"_resolved": {"deps": deps}} for deps in legs]}


def test_only_uncached_deps_are_waited_for_each_once() -> None:
    matrix = _matrix([_dep("eckit-gcc"), _dep("ecbuild", cached=True)], [_dep("eckit-gcc"), _dep("eckit-icpx")])
    assert wait_for_deps.pending(matrix) == [
        ("o/eckit", "a" * 40, "eckit-gcc"),
        ("o/eckit", "a" * 40, "eckit-icpx"),
    ]


def test_an_empty_matrix_waits_for_nothing() -> None:
    assert wait_for_deps.pending({"include": []}) == []
    assert wait_for_deps.pending(_matrix([])) == []


@pytest.mark.parametrize("available", [True, False])
def test_a_missing_dep_only_warns(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    polled: list[str] = []

    def poll(repo: str, sha: str, name: str, token: str | None) -> bool:
        polled.append(name)
        return available

    monkeypatch.setattr(wait_for_deps, "poll_for_artifact", poll)
    monkeypatch.setattr(wait_for_deps, "select_token", lambda: None)
    result = CliRunner().invoke(wait_for_deps.main, ["--matrix-json", json.dumps(_matrix([_dep("eckit-gcc")]))])
    assert result.exit_code == 0
    assert polled == ["eckit-gcc"]
    assert ("Still not in the store: eckit-gcc" in result.output) is not available
