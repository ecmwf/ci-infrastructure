# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
"""scripts/registry_cleanup.py: what prune and orphans would delete, without touching the registry."""

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
    spec = importlib.util.spec_from_file_location("registry_cleanup", REPO_ROOT / "scripts" / "registry_cleanup.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rc: Final = _load()

REPOS: Final[list[dict[str, Any]]] = [
    {
        "name": "public-ci-images/ubuntu24.04-base",
        "artifact_count": 3,
        "pull_count": 9,
        "update_time": "2026-09-18T00:00:00Z",
    },
    {
        "name": "public-ci-images/ubuntu24.04-gfortran13",
        "artifact_count": 2,
        "pull_count": 7,
        "update_time": "2026-09-17T00:00:00Z",
    },
    {
        "name": "public-ci-images/rocky8-old",
        "artifact_count": 1,
        "pull_count": 1,
        "update_time": "2026-09-19T00:00:00Z",
    },
]


def _artifact(day: int, *tags: str) -> dict[str, Any]:
    return {
        "digest": f"sha256:{day:064x}",
        "push_time": f"2026-09-{day:02d}T00:00:00.000Z",
        "tags": [{"name": t} for t in tags],
    }


def _names(artifacts: list[dict[str, Any]]) -> list[str]:
    return [",".join(rc.tag_names(a)) for a in artifacts]


# === orphans ===
def test_a_repository_with_no_dockerfile_is_an_orphan() -> None:
    orphans = rc.find_orphans(["ubuntu24.04/base"], REPOS)
    assert [rc.short_name(o["name"]) for o in orphans] == ["rocky8-old", "ubuntu24.04-gfortran13"]


def test_the_platform_slash_variant_maps_onto_the_flat_repository_name() -> None:
    assert rc.find_orphans(["ubuntu24.04/base", "ubuntu24.04/gfortran13", "rocky8/old"], REPOS) == []


def test_orphans_are_reported_newest_touched_first() -> None:
    orphans = rc.find_orphans([], REPOS)
    assert [o["update_time"] for o in orphans] == sorted((r["update_time"] for r in REPOS), reverse=True)


# === prune ===
def test_keeps_the_newest_by_push_time_whatever_the_order() -> None:
    arts = [_artifact(3, "c"), _artifact(10, "latest", "j"), _artifact(7, "g"), _artifact(1, "a")]
    assert _names(rc.stale_versions(arts, 2)) == ["c", "a"]


def test_latest_is_never_deleted_even_when_old() -> None:
    # A tag moved by hand, or a push that failed after tagging, can leave latest behind.
    arts = [_artifact(9, "i"), _artifact(8, "h"), _artifact(2, "latest", "b"), _artifact(1, "a")]
    assert _names(rc.stale_versions(arts, 2)) == ["a"]


def test_untagged_versions_go_too() -> None:
    arts = [_artifact(9, "i"), _artifact(8, "h"), _artifact(5)]
    assert _names(rc.stale_versions(arts, 2)) == [""]


@pytest.mark.parametrize("count", [0, 1, 2])
def test_nothing_to_prune_at_or_below_keep(count: int) -> None:
    assert rc.stale_versions([_artifact(d + 1, str(d)) for d in range(count)], 2) == []


# === report ===
@pytest.mark.parametrize(("task", "empty"), [("orphans", "No orphans"), ("prune", "Nothing to prune")])
@pytest.mark.parametrize("fmt", ["text", "md"])
def test_an_empty_plan_says_so_rather_than_printing_an_empty_table(task: str, empty: str, fmt: str) -> None:
    assert rc.render(task, [], fmt).startswith(empty)


def test_both_formats_name_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "enumerate_images", lambda: ["ubuntu24.04/base"])
    monkeypatch.setattr(rc, "list_repositories", lambda: REPOS)
    rows = rc.plan_orphans()
    for fmt in ("text", "md"):
        rendered = rc.render("orphans", rows, fmt)
        assert "rocky8-old" in rendered and "ubuntu24.04-gfortran13" in rendered


# === delete ===
def _prune_repo(monkeypatch: pytest.MonkeyPatch, days: tuple[int, ...]) -> list[str]:
    requests: list[str] = []
    monkeypatch.setattr(rc, "list_repositories", lambda: [{"name": "public-ci-images/x"}])
    monkeypatch.setattr(rc, "list_artifacts", lambda repo: [_artifact(d, str(d)) for d in days])
    monkeypatch.setattr(rc, "_request", lambda method, url, auth=None: requests.append(f"{method} {url}"))
    return requests


def test_delete_needs_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _prune_repo(monkeypatch, (1, 2, 3))
    monkeypatch.delenv("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", raising=False)
    monkeypatch.delenv("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", raising=False)
    assert rc.main(["prune", "--delete"]) == 2
    assert "needs PUBLIC_ECCR_CLEANUP_ROBOT_NAME" in capsys.readouterr().err


def test_prune_deletes_exactly_the_stale_digests(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _prune_repo(monkeypatch, (1, 2, 3, 4))
    monkeypatch.setenv("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", "robot")
    monkeypatch.setenv("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", "t")
    assert rc.main(["prune", "--delete"]) == 0
    base = f"{rc.API}/projects/{rc.PROJECT}/repositories/x/artifacts"
    assert requests == [f"DELETE {base}/{_artifact(2)['digest']}", f"DELETE {base}/{_artifact(1)['digest']}"]


def test_orphans_deletes_the_whole_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []
    monkeypatch.setattr(rc, "enumerate_images", lambda: ["ubuntu24.04/base", "ubuntu24.04/gfortran13"])
    monkeypatch.setattr(rc, "list_repositories", lambda: REPOS)
    monkeypatch.setattr(rc, "_request", lambda method, url, auth=None: requests.append(f"{method} {url}"))
    monkeypatch.setenv("PUBLIC_ECCR_CLEANUP_ROBOT_NAME", "robot")
    monkeypatch.setenv("PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN", "t")
    assert rc.main(["orphans", "--delete"]) == 0
    assert requests == [f"DELETE {rc.API}/projects/{rc.PROJECT}/repositories/rocky8-old"]


def test_keep_below_one_is_refused() -> None:
    with pytest.raises(SystemExit):
        rc.main(["prune", "--keep", "0"])
