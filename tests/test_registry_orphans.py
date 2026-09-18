# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
"""scripts/registry_orphans.py: the repository/image diff, without touching the registry."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("registry_orphans", REPO_ROOT / "scripts" / "registry_orphans.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ro: Final = _load()

REPOS: Final = [
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


def test_a_repository_with_no_dockerfile_is_an_orphan() -> None:
    orphans = ro.find_orphans(["ubuntu24.04/base"], REPOS)
    assert [ro.short_name(o["name"]) for o in orphans] == ["rocky8-old", "ubuntu24.04-gfortran13"]


def test_the_platform_slash_variant_maps_onto_the_flat_repository_name() -> None:
    assert ro.find_orphans(["ubuntu24.04/base", "ubuntu24.04/gfortran13", "rocky8/old"], REPOS) == []


def test_orphans_are_reported_newest_touched_first() -> None:
    orphans = ro.find_orphans([], REPOS)
    assert [o["update_time"] for o in orphans] == sorted((r["update_time"] for r in REPOS), reverse=True)


def test_nothing_to_report_says_so_rather_than_printing_an_empty_table() -> None:
    assert "No orphans" in ro.render([], "text")
    assert "No orphans" in ro.render([], "md")


def test_both_formats_name_every_orphan() -> None:
    orphans = ro.find_orphans(["ubuntu24.04/base"], REPOS)
    for fmt in ("text", "md"):
        rendered = ro.render(orphans, fmt)
        assert "rocky8-old" in rendered and "ubuntu24.04-gfortran13" in rendered
