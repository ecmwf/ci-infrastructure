# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""build-image.sh, run for real with a stub `docker` first on PATH."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPT: Final = REPO_ROOT / "build-image.sh"
PREFIX: Final = "eccr.ecmwf.int/public-ci-images"

# The script derives tags from git history; inside a CI image the checkout is a
# read-only mount owned by another user, where git refuses to run.
pytestmark = pytest.mark.skipif(
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


def test_validate_hands_a_rebuilt_base_to_its_dependents(tmp_path: Path) -> None:
    m = _discover(tmp_path, "validate")
    base = _by_name(m["base-matrix"])["rocky8/base"]
    dependent = _by_name(m["dependent-matrix"])["rocky8/gfortran8"]

    assert base["export"] is True
    assert base["ref"] == f"{PREFIX}/rocky8-base:{base['tag']}"
    assert dependent["base"] == "rocky8-base"
    assert dependent["base_ref"] == base["ref"]


def test_validate_bases_drops_dependents_of_rebuilt_bases(tmp_path: Path) -> None:
    m = _discover(tmp_path, "validate-bases")

    assert m["dependent-matrix"] == []
    assert m["base-matrix"]
    assert not any(e["export"] for e in m["base-matrix"])


def test_a_dependent_of_a_published_base_builds_on_latest(tmp_path: Path) -> None:
    m = _discover(tmp_path, "validate", PUBLISHED=f"rocky8-base:{_tag(tmp_path, 'rocky8/base')}")

    assert "rocky8/base" not in _by_name(m["base-matrix"])
    assert _by_name(m["dependent-matrix"])["rocky8/gfortran8"]["base_ref"] == ""


def test_base_image_overrides_the_literal_from_and_only_loads(tmp_path: Path) -> None:
    base_ref = f"{PREFIX}/rocky8-base:abc1234"
    result = _run(tmp_path, "rocky8/gfortran8", BASE_IMAGE=base_ref)
    assert result.returncode == 0, result.stderr

    argv = (tmp_path / "docker-args").read_text().splitlines()
    assert argv[argv.index("--build-context") + 1] == f"{PREFIX}/rocky8-base:latest=docker-image://{base_ref}"
    assert "--load" in argv
    assert "--push" not in argv


def test_base_image_is_rejected_for_a_base(tmp_path: Path) -> None:
    result = _run(tmp_path, "rocky8/base", BASE_IMAGE=f"{PREFIX}/rocky8-base:abc1234")

    assert result.returncode != 0
    assert "is not a dependent" in result.stderr


def test_push_built_refuses_an_image_that_was_not_built(tmp_path: Path) -> None:
    result = _run(tmp_path, "--push-built", "rocky8/base")

    assert result.returncode != 0
    assert "not in the local daemon" in result.stderr
