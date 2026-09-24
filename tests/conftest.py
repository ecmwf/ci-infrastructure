# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Throwaway repos with manifests in them, and stubs for gh and troika."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Final

import yaml

from ci_infrastructure._github_api import EXECUTION_RUNNER, Execution
from ci_infrastructure.generate_downstream_ci import Manifest, parse_manifest, render_workflow

_DEFAULT_PACKAGE: Final = """
    [package]
    name = "{name}"
    prefix = "{name}"
    repo = "org/{name}"
    compiler-inputs = []
"""


def write_repo(root: Path, repo_name: str, manifest_body: str) -> Path:
    """A body without its own [package] gets the default one for `repo_name`."""
    body = textwrap.dedent(manifest_body)
    if "[package]" not in body:
        body = textwrap.dedent(_DEFAULT_PACKAGE.format(name=repo_name)) + body
    manifest = root / repo_name / ".ci" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(body)
    return manifest


def discover_manifests(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*/.ci/manifest.toml") if p.is_file())


def parse_all(root: Path) -> list[Manifest]:
    return [parse_manifest(p) for p in discover_manifests(root)]


def render_single(root: Path, body: str, lane: Execution = EXECUTION_RUNNER, name: str = "a") -> str:
    """Write one repo and render its cross-repo-trigger workflow for `lane`."""
    write_repo(root, name, body)
    [m] = parse_all(root)
    out = render_workflow(m, {name: m}, lane=lane)
    assert out is not None
    return out


REPO_ROOT: Final = Path(__file__).resolve().parents[1]


def action_run_body(name: str) -> str:
    """The `run:` body of actions/<name>'s single composite step."""
    doc: dict[str, Any] = yaml.safe_load((REPO_ROOT / "actions" / name / "action.yml").read_text(encoding="utf-8"))
    (step,) = doc["runs"]["steps"]
    return str(step["run"])


def stub_gh(tmp_path: Path, body: str) -> Path:
    """Write an executable `gh` running `body` into tmp_path/bin; return that dir."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(f"#!/bin/sh\n{body}\n")
    (bindir / "gh").chmod(0o755)
    return bindir


class FakeProc:
    """A finished process, as troika's connection.execute() returns it."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, stderr: bytes = b"") -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr
