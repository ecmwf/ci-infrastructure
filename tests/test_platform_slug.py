# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The `platform` slot lets ABI-compatible images share one artifact; the image tag never enters the name."""

from __future__ import annotations

from typing import Final

import pytest

from ci_infrastructure._github_api import compute_platform_slug
from ci_infrastructure.resolve_deps import (
    PackageName,
    Sha,
    make_artifact_name,
    parse_manifest,
    producer_variants,
)

CLANG_IMG: Final = "registry.example/playground-ci/ubuntu24.04-clang18:0.2"


def test_platform_used_verbatim() -> None:
    assert compute_platform_slug("  ubuntu-24.04  ") == "ubuntu-24.04"


@pytest.mark.parametrize(
    ("platform", "match"), [("", "platform is required"), ("   ", "platform is required"), ("deadbeef-1", None)]
)
def test_platform_rejected(platform: str, match: str | None) -> None:
    """An 8-hex first segment would collide with the deps-hash8 slot."""
    with pytest.raises(ValueError, match=match):
        compute_platform_slug(platform)


_FORTMATH_MANIFEST: Final = """
[package]
name = "fortmath"
prefix = "fortmath"
repo = "owner/fortran"
compiler-inputs = ["fortran-compiler"]

[[matrix.build.include]]
fortran-compiler = "gfortran-13"
build-type = "Release"
runs-on = "arc-sandbox-cci2"
container = "registry.example/playground-ci/ubuntu24.04-gcc13-gfortran13:0.1"
platform = "ubuntu-24.04"
"""


def test_producer_variants_ignore_the_building_image() -> None:
    fortmath = parse_manifest(_FORTMATH_MANIFEST, default_repo="owner/fortran")
    assert [str(v) for v in producer_variants(fortmath, "fortmath") or []] == [
        "ubuntu-24.04-gfortran-13-Release (runner)"
    ]


def test_artifact_name_is_independent_of_the_building_image() -> None:
    assert (
        make_artifact_name(
            prefix=PackageName("fortmath"),
            sha=Sha("a" * 40),
            deps_hash8="1234abcd",
            platform_slug=compute_platform_slug("ubuntu-24.04"),
            compiler="gfortran-13",
            build_type="Release",
            python_version=None,
        )
        == "fortmath-" + ("a" * 40) + "-1234abcd-ubuntu-24.04-gfortran-13-Release"
    )
