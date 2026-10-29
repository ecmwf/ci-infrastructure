# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""A consumer repo with a release kind, shaped like eckit-cd-test, for the CD v3 tests."""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SAMPLE_WORKFLOW: Final = REPO_ROOT / "samples" / "cd" / "cd.yml"

RECIPE: Final = textwrap.dedent(
    """\
    {% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}
    {% block cmake_args %}
    -DCMAKE_C_FLAGS={{ c_flags | quote }} \\
    -DCMAKE_CXX_FLAGS={{ cxx_flags | quote }} \\
    {% endblock %}
    """
)

RPM_LEG: Final = """
[[matrix.release-rpm.include]]
job-script = "./.ci/build.sh.j2"
channel = "rpm"
platform = "rocky-9"
cxx-compiler = "g++-11"
c-compiler = "gcc-11"
container = "eccr.ecmwf.int/public-ci-images/rocky9-gcc11-gfortran11:latest"
runs-on = "arc-runner-very-large"
build-type = "Release"
options = "release"
install-prefix = "/opt/ecmwf"
arch = "x86_64"
nexus-target = "rocky-9"
install-test-image = "eccr.ecmwf.int/docker-hub-proxy/rockylinux/rockylinux:9"
install-test-command = "/opt/ecmwf/bin/pkg-version"
c-flags = "-fno-fast-math"
cxx-flags = "-fno-fast-math"
"""

MANIFEST: Final = """
[package]
name = "pkg"
repo = "o/pkg"
visibility = "{visibility}"
compiler-inputs = ["cxx-compiler"]

[release]
summary = "A package"
license = "Apache-2.0"
vendor = "ECMWF"

[[deps]]
repo = "o/ecbuild"
package = "ecbuild"
ref = "develop"
compiler-inputs = []
release-ref = "{ecbuild_ref}"

[[deps]]
repo = "o/stack"
package = ["libaec"]
ref = "master"
compiler-inputs = ["cxx-compiler"]
release-ref = false

[packages.pkg-release]
compiler-inputs = ["cxx-compiler"]

[[matrix.build.include]]
job-script = "./.ci/build.sh.j2"
platform = "rocky-9"
cxx-compiler = "g++-11"
c-compiler = "gcc-11"
build-type = "RelWithDebInfo"
c-flags = ""
cxx-flags = ""

[matrix.release-rpm]
packages = ["pkg-release"]
publishes = false
"""


def write_release_repo(
    root: Path,
    *,
    version: str = "2.5.0",
    ecbuild_ref: str = "3.16.0",
    leg: str = RPM_LEG,
    visibility: str = "private",
    workflow: str | None = None,
) -> Path:
    """The repo at `root`: manifest, recipe, VERSION (no newline) and .github/workflows/cd-v3.yml."""
    ci = root / ".ci"
    ci.mkdir(parents=True, exist_ok=True)
    manifest = ci / "manifest.toml"
    manifest.write_text(MANIFEST.format(ecbuild_ref=ecbuild_ref, visibility=visibility) + leg)
    (ci / "build.sh.j2").write_text(RECIPE)
    (root / "VERSION").write_text(version)
    wf = root / ".github" / "workflows" / "cd-v3.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    if workflow is None:
        shutil.copyfile(SAMPLE_WORKFLOW, wf)
    else:
        wf.write_text(workflow)
    return manifest
