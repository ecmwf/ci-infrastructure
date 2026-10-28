# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""fetch-deps puts only the deps that run here on LD_LIBRARY_PATH and PATH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT


@pytest.mark.parametrize(("stage_only", "exported"), [("false", True), ("true", False)])
def test_compose_exports_lib_dirs_only_for_deps_that_run_here(tmp_path: Path, stage_only: str, exported: bool) -> None:
    doc = yaml.safe_load((REPO_ROOT / "actions" / "fetch-deps" / "action.yml").read_text(encoding="utf-8"))
    (step,) = [s for s in doc["runs"]["steps"] if s.get("id") == "compose-cmake"]
    dep = tmp_path / "lz4"
    (dep / "lib").mkdir(parents=True)
    files = {k: tmp_path / k for k in ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH")}
    for f in files.values():
        f.touch()
    env = {
        "PATH": os.environ["PATH"],
        "CI_INFRASTRUCTURE_PYTHON": sys.executable,
        "DEPS_JSON": json.dumps([{"install-path": str(dep), "add-to-path": ["bin"]}]),
        "STAGE_ONLY": stage_only,
        **{k: str(v) for k, v in files.items()},
    }
    subprocess.run(["bash", "-c", step["run"]], env=env, check=True)
    assert files["GITHUB_OUTPUT"].read_text(encoding="utf-8") == f"cmake-prefix-path={dep}\n"
    assert (f"LD_LIBRARY_PATH={dep}/lib" in files["GITHUB_ENV"].read_text(encoding="utf-8")) is exported
    assert (f"{dep}/bin" in files["GITHUB_PATH"].read_text(encoding="utf-8")) is exported
