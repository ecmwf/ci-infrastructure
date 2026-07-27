"""Tests for the explicit `platform` artifact-name slot.

The platform key lets several ABI-compatible images (and a same-distro host
runner) share one artifact: a producer built under one image is reused by a
consumer building under a different image, instead of being rebuilt just
because the image tag differs. These tests pin that behaviour:

  - compute_platform_slug: explicit platform supersedes runs-on/container,
    legacy container/runs-on fallback still works, and the guards hold.
  - producer_can_build: an explicit platform makes runs-on/container stop
    discriminating, so two images on the same platform match.
  - end-to-end: the same fortmath artifact name is computed whether fortran
    builds it (gfortran13 image) or cxx resolves it as a dep (clang18 image).
"""

from __future__ import annotations

import pytest

from ci_infrastructure._github_api import compute_platform_slug
from ci_infrastructure.resolve_deps import (
    PackageName,
    Sha,
    make_artifact_name,
    parse_manifest,
    producer_can_build,
)

CLANG_IMG = "registry.example/playground-ci/ubuntu24.04-clang18-gfortran13:0.2"


def test_platform_used_verbatim() -> None:
    assert compute_platform_slug("ubuntu-24.04") == "ubuntu-24.04"
    assert compute_platform_slug("  ubuntu-24.04  ") == "ubuntu-24.04"


def test_platform_is_required() -> None:
    with pytest.raises(ValueError, match="platform is required"):
        compute_platform_slug("")
    with pytest.raises(ValueError, match="platform is required"):
        compute_platform_slug("   ")


def test_hex_collision_guard_still_applies() -> None:
    # An 8-hex first segment would collide with the deps-hash8 slot.
    with pytest.raises(ValueError):
        compute_platform_slug("deadbeef-1")


_FORTMATH_MANIFEST = """
[package]
name = "fortmath"
prefix = "fortmath"
repo = "owner/fortran"
compiler-inputs = ["fortran-compiler"]

[[matrix.build.include]]
fortran-compiler = "gfortran-13"
build-type = "Release"
runs-on = "arc-sandbox-cci2"
container = "registry.example/playground-ci/ubuntu24.04-gfortran13:0.1"
platform = "ubuntu-24.04"
"""


def test_producer_can_build_across_images_on_same_platform() -> None:
    fortmath = parse_manifest(_FORTMATH_MANIFEST, default_repo="owner/fortran")
    # cxx consumer builds under a DIFFERENT image but the SAME platform, and
    # also pins cxx-compiler (which fortmath does not declare).
    consumer = {
        "cxx-compiler": "clang++-18",
        "fortran-compiler": "gfortran-13",
        "build-type": "Release",
        "runs-on": "arc-sandbox-cci2",
        "container": CLANG_IMG,
        "platform": "ubuntu-24.04",
    }
    assert producer_can_build(fortmath, consumer)

    # A different Fortran compiler the producer can't satisfy still fails.
    bad = {**consumer, "fortran-compiler": "gfortran-12"}
    assert not producer_can_build(fortmath, bad)

    # A different platform also fails (the binary-compatibility class differs).
    other_platform = {**consumer, "platform": "ubuntu-22.04"}
    assert not producer_can_build(fortmath, other_platform)


def test_same_artifact_name_across_build_and_consume() -> None:
    """The crux: fortran building fortmath and cxx resolving the fortmath dep
    must compute the identical artifact name despite different images."""
    sha = Sha("a" * 40)
    deps_hash8 = "1234abcd"  # ecbuild dep hash; identical on both sides

    # Both sides declare platform="ubuntu-24.04" (fortran builds under the
    # gfortran13 image, cxx resolves the dep under the clang18 image) so the
    # slug — and therefore the whole artifact name — is identical.
    def name() -> str:
        return make_artifact_name(
            prefix=PackageName("fortmath"),
            sha=sha,
            deps_hash8=deps_hash8,
            platform_slug=compute_platform_slug("ubuntu-24.04"),
            compiler="gfortran-13",
            build_type="Release",
            python_version=None,
        )

    producer_name = name()  # fortran's own leg, gfortran13 image
    consumer_name = name()  # cxx resolving the fortmath dep, clang18 image
    assert producer_name == consumer_name == "fortmath-" + ("a" * 40) + "-1234abcd-ubuntu-24.04-gfortran-13-Release"
