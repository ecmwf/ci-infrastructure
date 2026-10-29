# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Fetch a release's source deps: each ``{repo, package, sha}`` the gate resolved, exactly that commit.

A source tree serves as a CMake prefix for a dep like ecbuild (it ships its package config).
Prints the ``cmake-prefix-path`` output: the checkouts, semicolon-separated.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import click

from .._github_api import write_outputs
from . import ReleaseError

_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE: Final = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_PACKAGE_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")


def _git(args: Sequence[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def validate(deps: Any) -> list[dict[str, str]]:
    if not isinstance(deps, list):
        raise ReleaseError(f"source deps must be a JSON list, got {type(deps).__name__}")
    out = []
    for d in deps:
        if not isinstance(d, dict):
            raise ReleaseError(f"source dep must be an object, got {d!r}")
        repo, package, sha = d.get("repo"), d.get("package"), d.get("sha")
        if not (isinstance(repo, str) and _REPO_RE.fullmatch(repo)):
            raise ReleaseError(f"source dep repo {repo!r} is not owner/name")
        if not (isinstance(package, str) and _PACKAGE_RE.fullmatch(package) and package not in (".", "..")):
            raise ReleaseError(f"source dep package {package!r} is not a plain name")
        if not (isinstance(sha, str) and _SHA_RE.fullmatch(sha)):
            raise ReleaseError(f"source dep {repo}: {sha!r} is not a 40-char commit SHA (the gate resolves refs)")
        out.append({"repo": repo, "package": package, "sha": sha})
    return out


def fetch(deps: Sequence[dict[str, str]], root: Path, *, server: str = "https://github.com") -> list[Path]:
    paths = []
    for d in deps:
        dest = root / d["package"]
        if dest.exists():
            raise ReleaseError(f"{dest} exists already; source deps go into a fresh directory")
        dest.mkdir(parents=True)
        _git(["init", "-q"], dest)
        _git(["remote", "add", "origin", f"{server}/{d['repo']}.git"], dest)
        _git(["fetch", "-q", "--depth", "1", "origin", d["sha"]], dest)
        _git(["checkout", "-q", "--detach", "FETCH_HEAD"], dest)
        head = _git(["rev-parse", "HEAD"], dest)
        if head != d["sha"]:
            raise ReleaseError(f"{d['repo']}: checked out {head}, expected {d['sha']}")
        print(f"{d['repo']} ({d['package']}) at {head} -> {dest}")
        paths.append(dest)
    return paths


@click.command("source-deps")
@click.option("--deps", "deps_json", required=True, help="JSON list of {repo, package, sha}, from the gate.")
@click.option("--root", type=click.Path(path_type=Path), required=True, help="A fresh directory to check out into.")
@click.option("--server", default="https://github.com", show_default=True, hidden=True)
def main(deps_json: str, root: Path, server: str) -> None:
    """Check out each source dep at its commit; output cmake-prefix-path."""
    paths = fetch(validate(json.loads(deps_json)), root, server=server)
    write_outputs({"cmake-prefix-path": ";".join(str(p) for p in paths)})
