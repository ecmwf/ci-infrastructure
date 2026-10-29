# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Release tags, VERSION, and the version each channel publishes under."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ci_infrastructure.release import ReleaseError
from ci_infrastructure.release.detect import classify
from ci_infrastructure.release.version import (
    ReleaseVersion,
    check_tag_matches_version,
    parse_tag,
    read_project_version,
    rehearsal_version,
)


@pytest.mark.parametrize(
    ("tag", "prefix", "parts", "mode", "rpm", "pypi", "suffix"),
    [
        ("2.5.1", "", 3, "release", "2.5.1", "2.5.1", "2.5.1"),
        ("2.5.1-rc1", "", 3, "prerelease", "2.5.1~rc1", "2.5.1rc1", "2.5.1-rc1"),
        ("2.5.1-RC3", "", 3, "prerelease", "2.5.1~rc3", "2.5.1rc3", "2.5.1-rc3"),
        ("v5.0.0-rc12", "v", 3, "prerelease", "5.0.0~rc12", "5.0.0rc12", "5.0.0-rc12"),
        ("6.0.0.1", "", 4, "release", "6.0.0.1", "6.0.0.1", "6.0.0.1"),
    ],
)
def test_tag_maps_to_channel_versions(
    tag: str, prefix: str, parts: int, mode: str, rpm: str, pypi: str, suffix: str
) -> None:
    v = parse_tag(tag, prefix=prefix, parts=parts)
    assert (v.mode, v.rpm_version, v.pypi_version, v.tag_suffix, v.tag) == (mode, rpm, pypi, suffix, tag)


@pytest.mark.parametrize(
    ("tag", "prefix", "parts"),
    [
        ("2.5.1-alpha", "", 3),  # only -rcN is a prerelease (Q7)
        ("2.5.1-beta1", "", 3),
        ("2.5", "", 3),
        ("2.5.1.0", "", 3),
        ("2.5.1", "", 4),
        ("v2.5.1", "", 3),
        ("2.5.1", "v", 3),
        ("02.5.1", "", 3),
        ("2.5.1-rc", "", 3),
        ("2.5.1-rc1 ", "", 3),
    ],
)
def test_non_release_tags_are_refused(tag: str, prefix: str, parts: int) -> None:
    with pytest.raises(ReleaseError, match="not a release tag"):
        parse_tag(tag, prefix=prefix, parts=parts)


def test_bad_prefix_or_parts_are_refused() -> None:
    with pytest.raises(ReleaseError, match="tag prefix"):
        parse_tag("r1.0.0", prefix="r")
    with pytest.raises(ReleaseError, match="version parts"):
        parse_tag("1.0", parts=2)


def test_version_file_is_stripped_and_primary(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("2.5.0")  # eckit's has no trailing newline
    (tmp_path / "CMakeLists.txt").write_text("project( x VERSION 9.9.9 )")
    assert read_project_version(tmp_path) == "2.5.0"
    (tmp_path / "VERSION").write_text(" 2.5.0\n\n")
    assert read_project_version(tmp_path) == "2.5.0"


def test_cmake_project_version_without_a_version_file(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project( foo\n  VERSION 1.2.3\n  LANGUAGES CXX )\n")
    assert read_project_version(tmp_path) == "1.2.3"
    (tmp_path / "CMakeLists.txt").write_text("project( eckit LANGUAGES CXX )\n")
    with pytest.raises(ReleaseError, match="no VERSION file"):
        read_project_version(tmp_path)


def test_tag_must_name_the_project_version() -> None:
    check_tag_matches_version(parse_tag("2.5.0-rc1"), "2.5.0")
    with pytest.raises(ReleaseError, match="bump VERSION"):
        check_tag_matches_version(parse_tag("2.5.1-rc1"), "2.5.0")


def test_rehearsal_version_is_the_project_version() -> None:
    v = rehearsal_version("2.5.0")
    assert (v.mode, v.rpm_version, v.tag) == ("rehearsal", "2.5.0", None)
    with pytest.raises(ReleaseError):
        rehearsal_version("2.5.0-dev")


# Reference ordering, as rpm and dpkg compare: an rc sorts before its release.
_ORDER = ["2.4.9", "2.5.0~rc1", "2.5.0~rc2", "2.5.0~rc10", "2.5.0", "2.5.1~rc1", "2.5.1"]


def test_rc_versions_sort_before_the_release_in_the_reference_table() -> None:
    versions = [parse_tag(t) for t in ("2.5.0-rc1", "2.5.0-rc2", "2.5.0-rc10", "2.5.0", "2.5.1-rc1", "2.5.1")]
    assert [v.rpm_version for v in versions] == _ORDER[1:]


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg not installed")
def test_rc_versions_sort_before_the_release_by_dpkg() -> None:
    for lower, higher in zip(_ORDER, _ORDER[1:]):
        assert subprocess.run(["dpkg", "--compare-versions", lower, "lt", higher]).returncode == 0, (lower, higher)


def test_classify_tag_push() -> None:
    e = classify(
        event_name="push",
        ref="refs/tags/2.5.0-RC1",
        sha="a" * 40,
        repository="o/r",
        payload={},
        project_version="2.5.0",
    )
    assert (e.mode, e.version.rpm_version, e.sha, e.head_sha) == ("prerelease", "2.5.0~rc1", "a" * 40, "a" * 40)


def test_classify_branch_push_is_refused() -> None:
    with pytest.raises(ReleaseError, match="tag pushes"):
        classify(
            event_name="push",
            ref="refs/heads/main",
            sha="a" * 40,
            repository="o/r",
            payload={},
            project_version="1.0.0",
        )


def _pr(label: str = "approved-for-cd", head_repo: str = "o/r", action: str = "labeled") -> dict[str, object]:
    return {
        "action": action,
        "label": {"name": label},
        "pull_request": {"number": 7, "head": {"sha": "b" * 40, "ref": "feature/x", "repo": {"full_name": head_repo}}},
    }


def test_classify_labelled_same_repo_pr_is_a_rehearsal_of_the_merge_commit() -> None:
    e = classify(
        event_name="pull_request",
        ref="refs/pull/7/merge",
        sha="c" * 40,
        repository="o/r",
        payload=_pr(),
        project_version="2.5.0",
    )
    assert (e.mode, e.sha, e.head_sha, e.pr_number, e.head_ref) == ("rehearsal", "c" * 40, "b" * 40, 7, "feature/x")
    assert e.version == ReleaseVersion(base="2.5.0")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (_pr(label="run-ci"), "does not start a rehearsal"),
        (_pr(head_repo="fork/r"), "no rehearsal from a fork"),
        (_pr(action="opened"), "'labeled' only"),
    ],
)
def test_classify_refuses_other_pull_requests(payload: dict[str, object], match: str) -> None:
    with pytest.raises(ReleaseError, match=match):
        classify(
            event_name="pull_request", ref="r", sha="c" * 40, repository="o/r", payload=payload, project_version="1.0.0"
        )


def test_classify_refuses_other_events() -> None:
    with pytest.raises(ReleaseError, match="does not start a release"):
        classify(
            event_name="workflow_run", ref="r", sha="c" * 40, repository="o/r", payload={}, project_version="1.0.0"
        )
