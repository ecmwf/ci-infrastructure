# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
"""scripts/registry_prune.py: which versions go, without touching the registry."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("registry_prune", REPO_ROOT / "scripts" / "registry_prune.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp: Final = _load()


def _artifact(day: int, *tags: str) -> dict[str, Any]:
    return {
        "digest": f"sha256:{day:064x}",
        "push_time": f"2026-09-{day:02d}T00:00:00.000Z",
        "size": 2**20,
        "tags": [{"name": t} for t in tags],
    }


def _names(artifacts: list[dict[str, Any]]) -> list[str]:
    return [",".join(rp.tag_names(a)) for a in artifacts]


def test_keeps_the_newest_by_push_time_whatever_the_order() -> None:
    arts = [_artifact(3, "c"), _artifact(10, "latest", "j"), _artifact(7, "g"), _artifact(1, "a")]
    assert _names(rp.stale(arts, 2)) == ["c", "a"]


def test_latest_is_never_deleted_even_when_old() -> None:
    # A tag moved by hand, or a push that failed after tagging, can leave latest behind.
    arts = [_artifact(9, "i"), _artifact(8, "h"), _artifact(2, "latest", "b"), _artifact(1, "a")]
    assert _names(rp.stale(arts, 2)) == ["a"]


def test_untagged_versions_go_too() -> None:
    arts = [_artifact(9, "i"), _artifact(8, "h"), _artifact(5)]
    assert _names(rp.stale(arts, 2)) == [""]


@pytest.mark.parametrize("count", [0, 1, 2])
def test_nothing_to_do_at_or_below_keep(count: int) -> None:
    assert rp.stale([_artifact(d + 1, str(d)) for d in range(count)], 2) == []


def test_render_reports_nothing_to_prune() -> None:
    assert rp.render({}, 2, "text").startswith("Nothing to prune")


def test_render_markdown_lists_each_version() -> None:
    out = rp.render({"ubuntu24.04-base": [_artifact(3, "abc1234")]}, 2, "md")
    assert "| `ubuntu24.04-base` | abc1234 | 2026-09-03 |" in out.splitlines()


def test_delete_needs_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(rp, "list_repositories", lambda: [{"name": "public-ci-images/x"}])
    monkeypatch.setattr(rp, "list_artifacts", lambda repo: [_artifact(d, str(d)) for d in (1, 2, 3)])
    monkeypatch.delenv("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", raising=False)
    monkeypatch.delenv("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", raising=False)
    assert rp.main(["--delete"]) == 2
    assert "needs PUBLIC_ECCR_CLEANUP_ROBOT_NAME" in capsys.readouterr().err


def test_delete_removes_exactly_the_stale_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(rp, "list_repositories", lambda: [{"name": "public-ci-images/x"}])
    monkeypatch.setattr(rp, "list_artifacts", lambda repo: [_artifact(d, str(d)) for d in (1, 2, 3, 4)])
    monkeypatch.setattr(rp, "delete_artifact", lambda repo, digest, auth: deleted.append((repo, digest)))
    monkeypatch.setenv("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", "robot")
    monkeypatch.setenv("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", "t")
    assert rp.main(["--delete"]) == 0
    assert deleted == [("x", _artifact(2)["digest"]), ("x", _artifact(1)["digest"])]
