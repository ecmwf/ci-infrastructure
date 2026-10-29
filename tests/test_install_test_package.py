# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""actions/install-test-package's script, with dnf and rpm stubbed: what it checks, and its receipt."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION = REPO_ROOT / "actions" / "install-test-package"
FILENAME = "pkg-2.5.0~rc1-1.x86_64.rpm"
SHA = "0123456789abcdef0123456789abcdef01234567"
JOB_KEY = "pkg-2.5.0-rc1-rpm-rocky-9-gxx-11-01234567"


def _build_meta(**release: Any) -> str:
    """As the release template writes it: _release.build_meta under "release", then more fields."""
    meta = {"head_sha": "f" * 40, "job_key": JOB_KEY, "leg_id": "rpm-rocky-9-gxx-11", "sha": SHA, "tag": "2.5.0-rc1"}
    meta.update(release)
    return '{\n"release": ' + json.dumps(meta, indent=2, sort_keys=True) + ',\n"sha256": "x"\n}\n'


def _script() -> str:
    doc = yaml.safe_load((ACTION / "action.yml").read_text())
    [step] = doc["runs"]["steps"]
    assert isinstance(step["run"], str)
    return step["run"]


def _setup(tmp_path: Path, *, listed: str = "", sums: str | None = None) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"dnf": 'echo "dnf $*" >> "$STUB_LOG"', "rpm": 'printf "%s\\n" $STUB_RPM_FILES'}.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/bash\n{body}\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    package = tmp_path / "package"
    package.mkdir()
    (package / FILENAME).write_bytes(b"rpm-bytes")
    digest = hashlib.sha256(b"rpm-bytes").hexdigest()
    (package / "SHA256SUMS").write_text(sums if sums is not None else f"{digest}  {FILENAME}\n")
    (package / "build-meta.json").write_text(_build_meta())
    prefix = tmp_path / "opt" / "ecmwf"
    (prefix / "lib" / "cmake").mkdir(parents=True)
    (prefix / "lib" / "cmake" / "pkg-config.cmake").write_text(listed)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GITHUB_ACTION_PATH": str(ACTION),
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "STUB_LOG": str(tmp_path / "dnf.log"),
        "STUB_RPM_FILES": f"{tmp_path}/opt {prefix} {prefix}/lib/cmake/pkg-config.cmake",
        "PACKAGE_DIR": str(package),
        "FILENAME": FILENAME,
        "PREFIX": str(prefix),
        "SMOKE_COMMAND": f"test -f {prefix}/lib/cmake/pkg-config.cmake",
        "LEG_ID": "rpm-rocky-9-gxx-11",
        "IMAGE": "eccr.ecmwf.int/docker-hub-proxy/rockylinux/rockylinux:9",
        "OUTPUT_DIR": str(tmp_path / "itest"),
    }


def _run(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", _script()], env=env, cwd=cwd, capture_output=True, text=True)


def test_a_clean_package_passes_and_records_its_digest(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    result = _run(env, tmp_path)
    assert result.returncode == 0, result.stderr
    receipt: dict[str, Any] = json.loads((tmp_path / "itest" / "itest-rpm-rocky-9-gxx-11.json").read_text())
    assert receipt["result"] == "passed"
    assert receipt["sha256"] == hashlib.sha256(b"rpm-bytes").hexdigest()
    assert receipt["filename"] == FILENAME
    assert (receipt["sha"], receipt["job_key"]) == (SHA, JOB_KEY)
    assert (tmp_path / "dnf.log").read_text().splitlines() == [
        "dnf -y install epel-release",
        f"dnf -y install binutils ./{FILENAME}",
    ]
    assert (tmp_path / "out").read_text() == f"receipt={tmp_path}/itest/itest-rpm-rocky-9-gxx-11.json\n"


def test_a_digest_mismatch_fails_before_installing(tmp_path: Path) -> None:
    env = _setup(tmp_path, sums=f"{'0' * 64}  {FILENAME}\n")
    result = _run(env, tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "dnf.log").exists()


def test_a_package_missing_from_sha256sums_fails(tmp_path: Path) -> None:
    env = _setup(tmp_path, sums="")
    assert _run(env, tmp_path).returncode != 0


def test_findings_fail_without_a_receipt(tmp_path: Path) -> None:
    env = _setup(tmp_path, listed='set(X "/__w/pkg/pkg/build")\n')
    env["STUB_RPM_FILES"] += " /usr/bin/stray"
    env["SMOKE_COMMAND"] = "false"
    result = _run(env, tmp_path)
    assert result.returncode != 0
    assert "/usr/bin/stray is outside" in result.stderr
    assert "build paths leaked" in result.stderr
    assert not list((tmp_path / "itest").glob("*.json"))


def test_only_rpms(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    env["FILENAME"] = "pkg.deb"
    assert "rpms only" in _run(env, tmp_path).stderr


def test_a_package_without_its_build_identity_fails(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    meta = tmp_path / "package" / "build-meta.json"
    for broken in ("", _build_meta(sha="not-a-sha"), _build_meta(job_key="a b")):
        meta.write_text(broken)
        result = _run(env, tmp_path)
        assert result.returncode != 0 and "no single valid" in result.stderr, broken
        assert not (tmp_path / "dnf.log").exists()
    meta.unlink()
    assert _run(env, tmp_path).returncode != 0
