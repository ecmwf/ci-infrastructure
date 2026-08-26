# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for building throwaway repos with manifests in them.

The generator's tests all work the same way: materialise one or more fake repos
under a tmp_path, parse them, render, and assert on the YAML. These helpers own
the boilerplate so a test body is only the manifest fields it is actually about.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Final

from ci_infrastructure.generate_downstream_ci import Manifest, parse_manifest

#: The [package] block almost every fixture needs and no test is about. A body
#: passed to `write_repo` that already opens with [package] keeps its own.
_DEFAULT_PACKAGE: Final = """
    [package]
    name = "{name}"
    prefix = "{name}"
    repo = "org/{name}"
    compiler-inputs = []
"""


def write_repo(root: Path, repo_name: str, manifest_body: str) -> Path:
    """Materialise a fake repo with .ci/manifest.toml under root; return its path.

    A body that does not declare its own [package] gets the default one for
    `repo_name` prepended, so tests about matrix kinds do not restate it.
    """
    body = textwrap.dedent(manifest_body)
    if "[package]" not in body:
        body = textwrap.dedent(_DEFAULT_PACKAGE.format(name=repo_name)) + body
    manifest = root / repo_name / ".ci" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(body)
    return manifest


def discover_manifests(root: Path) -> list[Path]:
    """Every `*/.ci/manifest.toml` directly under `root`.

    Only tests lay repos out as sibling directories like this; production reads
    the one manifest in the cwd and fetches the rest over the GitHub API.
    """
    return sorted(p for p in root.glob("*/.ci/manifest.toml") if p.is_file())


def parse_all(root: Path) -> list[Manifest]:
    return [parse_manifest(p) for p in discover_manifests(root)]
