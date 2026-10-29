# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Release tags, the VERSION file, and the version each channel publishes under.

A tag is ``<prefix>X.Y.Z[.W]`` with an optional ``-rc<N>``, case-insensitive (``-RC1`` too).
Only ``-rcN`` is a prerelease; ``-alpha``, ``-beta`` and the like are not release tags.

- release: tag ``2.5.1``, rpm ``2.5.1-1``, VERSION ``2.5.1``;
- prerelease: tag ``2.5.1-rc1`` (or ``-RC1``), rpm ``2.5.1~rc1-1``, VERSION ``2.5.1``;
- rehearsal: no tag, rpm ``2.5.1-1`` (never published), VERSION ``2.5.1``.

The rc rpm sorts before the release (``~``). v2 published rc rpms under the plain VERSION.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from . import ReleaseError

Mode = Literal["release", "prerelease", "rehearsal"]
MODE_RELEASE: Final[Mode] = "release"
MODE_PRERELEASE: Final[Mode] = "prerelease"
MODE_REHEARSAL: Final[Mode] = "rehearsal"

_NUM: Final = r"(?:0|[1-9][0-9]*)"
_CMAKE_PROJECT_VERSION_RE: Final = re.compile(r"project\s*\([^)]*?\bVERSION\s+([0-9]+(?:\.[0-9]+){1,3})", re.I | re.S)

#: The rpm/deb Release field: the first build of a version. No rebuilds under one version (immutable).
PACKAGE_RELEASE: Final = "1"


def tag_pattern(prefix: str = "", parts: int = 3) -> re.Pattern[str]:
    if prefix not in ("", "v"):
        raise ReleaseError(f"tag prefix must be '' or 'v', got {prefix!r}")
    if parts not in (3, 4):
        raise ReleaseError(f"version parts must be 3 or 4, got {parts!r}")
    base = r"\.".join([_NUM] * parts)
    return re.compile(rf"^{re.escape(prefix)}(?P<base>{base})(?:-rc(?P<rc>{_NUM}))?$", re.I)


@dataclass(frozen=True)
class ReleaseVersion:
    """``base`` = the VERSION file's value; ``rc`` = N of ``-rcN``, None for a release."""

    base: str
    rc: int | None = None
    tag: str | None = None

    @property
    def mode(self) -> Mode:
        if self.tag is None:
            return MODE_REHEARSAL
        return MODE_RELEASE if self.rc is None else MODE_PRERELEASE

    @property
    def rpm_version(self) -> str:
        """rpm ``Version``: ``2.5.1~rc1`` sorts before ``2.5.1``."""
        return self.base if self.rc is None else f"{self.base}~rc{self.rc}"

    @property
    def deb_version(self) -> str:
        return self.rpm_version

    @property
    def pypi_version(self) -> str:
        return self.base if self.rc is None else f"{self.base}rc{self.rc}"

    @property
    def tag_suffix(self) -> str:
        """The tag without its prefix, lower-cased: what keys a run (``2.5.1-rc1``)."""
        return self.base if self.rc is None else f"{self.base}-rc{self.rc}"


def parse_tag(tag: str, *, prefix: str = "", parts: int = 3) -> ReleaseVersion:
    m = tag_pattern(prefix, parts).fullmatch(tag)
    if m is None:
        example = f"{prefix}{'.'.join(['1'] * parts)}"
        raise ReleaseError(
            f"tag {tag!r} is not a release tag: expected {example} or {example}-rc1 "
            f"(prefix {prefix!r}, {parts} version parts; only -rcN marks a prerelease)"
        )
    rc = m.group("rc")
    return ReleaseVersion(base=m.group("base"), rc=None if rc is None else int(rc), tag=tag)


def read_project_version(repo_root: Path) -> str:
    """The ``VERSION`` file, stripped; without one, ``project(... VERSION x.y.z)`` in CMakeLists.txt."""
    version_file = repo_root / "VERSION"
    if version_file.is_file():
        version = version_file.read_text().strip()
        if not version:
            raise ReleaseError(f"{version_file} is empty")
        return version
    cmakelists = repo_root / "CMakeLists.txt"
    if cmakelists.is_file() and (m := _CMAKE_PROJECT_VERSION_RE.search(cmakelists.read_text())):
        return m.group(1)
    raise ReleaseError(f"no VERSION file in {repo_root}, and CMakeLists.txt declares no project VERSION")


def check_tag_matches_version(version: ReleaseVersion, project_version: str) -> None:
    if version.base != project_version:
        raise ReleaseError(
            f"tag {version.tag!r} is version {version.base}, but the project says {project_version}: "
            "bump VERSION in the tagged commit, or tag the version it has"
        )


def rehearsal_version(project_version: str, *, parts: int = 3) -> ReleaseVersion:
    if not re.fullmatch(r"\.".join([_NUM] * parts), project_version):
        raise ReleaseError(f"project version {project_version!r} is not {parts} dot-separated numbers")
    return ReleaseVersion(base=project_version)
