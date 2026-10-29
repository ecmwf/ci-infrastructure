# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""release-source-deps: each dep at exactly the commit the gate resolved."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from ci_infrastructure.release import ReleaseError, source_deps


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def server(tmp_path: Path) -> tuple[Path, str, str]:
    """A local "server" holding o/ecbuild with two commits; returns (root, first sha, second sha)."""
    repo = tmp_path / "server" / "o" / "ecbuild.git"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    for n in (1, 2):
        (repo / "VERSION").write_text(f"{n}\n")
        _git(repo, "add", "VERSION")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"c{n}")
        if n == 1:
            first = _git(repo, "rev-parse", "HEAD")
    return tmp_path / "server", first, _git(repo, "rev-parse", "HEAD")


def test_fetch_checks_out_the_resolved_commit_not_the_tip(tmp_path: Path, server: tuple[Path, str, str]) -> None:
    root, first, _ = server
    deps = source_deps.validate([{"repo": "o/ecbuild", "package": "ecbuild", "sha": first}])
    [path] = source_deps.fetch(deps, tmp_path / "deps", server=f"file://{root}")
    assert path == tmp_path / "deps" / "ecbuild"
    assert _git(path, "rev-parse", "HEAD") == first
    assert (path / "VERSION").read_text() == "1\n"


def test_cli_writes_the_cmake_prefix_path(
    tmp_path: Path, server: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, second = server
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    deps = json.dumps([{"repo": "o/ecbuild", "package": "ecbuild", "sha": second, "ref": "x"}])
    result = CliRunner().invoke(
        source_deps.main, ["--deps", deps, "--root", str(tmp_path / "deps"), "--server", f"file://{root}"]
    )
    assert result.exit_code == 0, result.output
    assert out.read_text() == f"cmake-prefix-path={tmp_path / 'deps' / 'ecbuild'}\n"


@pytest.mark.parametrize(
    ("deps", "match"),
    [
        ({"repo": "o/e"}, "JSON list"),
        ([{"repo": "o/e", "package": "e", "sha": "develop"}], "40-char commit SHA"),
        ([{"repo": "o", "package": "e", "sha": "a" * 40}], "owner/name"),
        ([{"repo": "o/e", "package": "../x", "sha": "a" * 40}], "plain name"),
    ],
)
def test_validate_refuses(deps: object, match: str) -> None:
    with pytest.raises(ReleaseError, match=match):
        source_deps.validate(deps)


def test_an_existing_checkout_is_not_reused(tmp_path: Path, server: tuple[Path, str, str]) -> None:
    root, first, _ = server
    (tmp_path / "deps" / "ecbuild").mkdir(parents=True)
    with pytest.raises(ReleaseError, match="exists already"):
        source_deps.fetch([{"repo": "o/ecbuild", "package": "ecbuild", "sha": first}], tmp_path / "deps")
