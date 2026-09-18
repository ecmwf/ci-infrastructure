# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""scripts/build_image.py: in-process, and through build-image.sh with a stub `docker` first on PATH."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPT: Final = REPO_ROOT / "build-image.sh"
PREFIX: Final = "eccr.ecmwf.int/public-ci-images"

# The script derives tags from git history; inside a CI image the checkout is a
# read-only mount owned by another user, where git refuses to run.
needs_git = pytest.mark.skipif(
    subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True).returncode != 0,
    reason="needs a usable git checkout",
)

# Registry: a ref exists when its last path component is in $PUBLISHED.
# Daemon: an image exists when its ref is in $LOCAL. Builds record their argv.
_STUB: Final = """#!/bin/sh
case "$1 $2" in
  "buildx version") exit 0 ;;
  "buildx imagetools")
    for last; do :; done
    case " $PUBLISHED " in *" ${last##*/} "*) exit 0 ;; esac
    echo "not found"; exit 1 ;;
  "buildx build") shift 2; printf '%s\\n' "$@" > "$DOCKER_ARGS"; exit 0 ;;
  "image inspect")
    for last; do :; done
    case " $LOCAL " in *" $last "*) exit 0 ;; esac
    exit 1 ;;
esac
exit 0
"""


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(_STUB)
    (bindir / "docker").chmod(0o755)
    base_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "IMAGE_TAG", "BASE_IMAGE"}
        and not k.startswith("PUBLIC_ECCR_")
    }
    full_env = base_env | {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHON": sys.executable,
        "DOCKER_ARGS": str(tmp_path / "docker-args"),
        "PUBLISHED": "",
        "LOCAL": "",
    }
    return subprocess.run(
        [str(SCRIPT), *args], cwd=REPO_ROOT, env=full_env | env, capture_output=True, text=True, check=False
    )


def _tag(tmp_path: Path, name: str) -> str:
    return _run(tmp_path, "--print-tag", name).stdout.strip()


def _discover(tmp_path: Path, mode: str, **env: str) -> dict[str, list[dict[str, Any]]]:
    result = _run(tmp_path, "--discover", "--mode", mode, **env)
    assert result.returncode == 0, result.stderr
    matrices = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in {"base-matrix", "dependent-matrix"}:
            matrices[key] = json.loads(value)["include"]
    return matrices


def _by_name(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in entries}


@needs_git
def test_validate_hands_a_rebuilt_base_to_its_dependents(tmp_path: Path) -> None:
    m = _discover(tmp_path, "validate")
    base = _by_name(m["base-matrix"])["rocky8/base"]
    dependent = _by_name(m["dependent-matrix"])["rocky8/gcc8-gfortran8"]

    assert base["export"] is True
    assert base["ref"] == f"{PREFIX}/rocky8-base:{base['tag']}"
    assert dependent["base"] == "rocky8-base"
    assert dependent["base_ref"] == base["ref"]


@needs_git
def test_validate_bases_drops_dependents_of_rebuilt_bases(tmp_path: Path) -> None:
    m = _discover(tmp_path, "validate-bases")

    assert m["dependent-matrix"] == []
    assert m["base-matrix"]
    assert not any(e["export"] for e in m["base-matrix"])


@needs_git
def test_a_dependent_of_a_published_base_builds_on_latest(tmp_path: Path) -> None:
    m = _discover(tmp_path, "validate", PUBLISHED=f"rocky8-base:{_tag(tmp_path, 'rocky8/base')}")

    assert "rocky8/base" not in _by_name(m["base-matrix"])
    assert _by_name(m["dependent-matrix"])["rocky8/gcc8-gfortran8"]["base_ref"] == ""


@needs_git
def test_base_image_overrides_the_literal_from_and_only_loads(tmp_path: Path) -> None:
    base_ref = f"{PREFIX}/rocky8-base:abc1234"
    result = _run(tmp_path, "rocky8/gcc8-gfortran8", BASE_IMAGE=base_ref)
    assert result.returncode == 0, result.stderr

    argv = (tmp_path / "docker-args").read_text().splitlines()
    assert argv[argv.index("--build-context") + 1] == f"{PREFIX}/rocky8-base:latest=docker-image://{base_ref}"
    assert "--load" in argv
    assert "--push" not in argv


@needs_git
def test_base_image_is_rejected_for_a_base(tmp_path: Path) -> None:
    result = _run(tmp_path, "rocky8/base", BASE_IMAGE=f"{PREFIX}/rocky8-base:abc1234")

    assert result.returncode != 0
    assert "is not a dependent" in result.stderr


@needs_git
def test_push_built_refuses_an_image_that_was_not_built(tmp_path: Path) -> None:
    result = _run(tmp_path, "--push-built", "rocky8/base")

    assert result.returncode != 0
    assert "not in the local daemon" in result.stderr


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_image", REPO_ROOT / "scripts" / "build_image.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bi: Final = _load()


def _image(root: Path, name: str, dockerfile: str) -> None:
    (root / "public-images" / name).mkdir(parents=True)
    (root / "public-images" / name / "Dockerfile").write_text(dockerfile)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bi, "REPO_ROOT", tmp_path)
    return tmp_path


def test_the_longest_platform_prefix_claims_the_base(tree: Path) -> None:
    _image(tree, "ubuntu24.04/x-base", "FROM ubuntu:24.04\n")
    _image(tree, "ubuntu24.04-x/base", "FROM ubuntu:24.04\n")
    _image(tree, "ubuntu24.04-x/gfortran", f"FROM {PREFIX}/ubuntu24.04-x-base:latest\n")

    assert bi.resolve_base("ubuntu24.04-x/gfortran") == "ubuntu24.04-x/base"
    assert bi.resolve_base("ubuntu24.04-x/base") == ""


@pytest.mark.parametrize(
    ("dockerfile", "message"),
    [
        (f"FROM {PREFIX}/a-base:latest AS builder\n", "multi-stage"),
        ("ARG V\nFROM ubuntu:$V\n", "variable FROM"),
        ("FROM eccr.ecmwf.int/other/a-base:latest\n", "not in project"),
        ("RUN true\n", "no FROM line"),
        (f"FROM {PREFIX}/missing-base:latest\n", "no matching"),
    ],
)
def test_unsupported_from_lines_are_refused(tree: Path, dockerfile: str, message: str) -> None:
    _image(tree, "a/gfortran", dockerfile)

    with pytest.raises(bi.BuildImageError, match=message):
        bi.resolve_base("a/gfortran")


def test_rolling_tags_carry_a_utc_date_and_image_tag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_TAG", raising=False)
    monkeypatch.setattr(bi, "git_identity", lambda name, fmt: "abc1234")

    assert bi.compute_tag("ubuntu24.04/base") == "abc1234"
    assert re.fullmatch(r"abc1234-\d{8}", bi.compute_tag("rolling-arch/base"))
    assert re.fullmatch(r"abc1234-\d{8}", bi.compute_tag("rolling/base"))
    assert bi.compute_tag("rollingstone/base") == "abc1234"

    monkeypatch.setenv("IMAGE_TAG", "pinned")
    assert bi.compute_tag("rolling-arch/base") == "pinned"


@pytest.mark.parametrize(
    ("returncode", "output", "exists"),
    [
        (0, "", True),
        (1, "ERROR: MANIFEST_UNKNOWN: manifest unknown", False),
        (1, "failed to resolve: not found", False),
        (1, "dial tcp: connection refused", None),
    ],
)
def test_an_unreachable_registry_is_not_a_missing_tag(
    monkeypatch: pytest.MonkeyPatch, returncode: int, output: str, exists: bool | None
) -> None:
    result = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=output)
    monkeypatch.setattr(bi.subprocess, "run", lambda *a, **k: result)

    if exists is None:
        with pytest.raises(bi.BuildImageError, match="cannot reach"):
            bi.image_exists("ref")
    else:
        assert bi.image_exists("ref") is exists


def test_build_args_are_passed_only_when_declared(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text('FROM x\n  ARG SOURCE_REVISION=""\nARG IMAGE_NAMES\nARG IMAGE_TAG\n# ARG IMAGE_CREATED\n')

    assert bi.declared_args(dockerfile) == ["SOURCE_REVISION", "IMAGE_TAG"]


def test_discover_refuses_a_variant_on_a_variant(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _image(tree, "p/base", "FROM ubuntu:24.04\n")
    _image(tree, "p/gfortran", f"FROM {PREFIX}/p-base:latest\n")
    _image(tree, "p/gfortran-qt", f"FROM {PREFIX}/p-gfortran:latest\n")
    monkeypatch.setattr(bi, "require_buildx", lambda: None)
    monkeypatch.setattr(bi, "compute_tag", lambda name: "t")
    monkeypatch.setattr(bi, "image_exists", lambda ref: False)

    with pytest.raises(bi.BuildImageError, match="chains deeper"):
        bi.discover("validate", "")
