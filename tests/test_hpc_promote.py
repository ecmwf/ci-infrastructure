# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""hpc-promote, with this machine as the cluster: once, atomically, never over another tree."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest
from test_hpc_release_archive import LocalConn, hpc_leg

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import promote, release_tree, transfer


@pytest.fixture(autouse=True)
def zstd(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a real zstd, a "compressed" archive is the plain tar."""
    if REAL_ZSTD:
        return
    bin_dir = tmp_path_factory.mktemp("bin")
    stub = bin_dir / "zstd"
    stub.write_text(
        "#!/bin/bash\n"
        'out=""; dc=0; file=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o) out="$2"; shift ;; -dc) dc=1 ;; -q|-T0) ;; *) file="$1" ;; esac\n'
        "  shift\n"
        "done\n"
        'if [ "$dc" = 1 ]; then cat "$file"; else cat > "$out"; fi\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


#: Looked up before the stub goes on PATH.
REAL_ZSTD = shutil.which("zstd")


def _compress(plain: bytes) -> bytes:
    if REAL_ZSTD:
        return subprocess.run([REAL_ZSTD, "-q", "-c"], input=plain, capture_output=True, check=True).stdout
    return plain


def _tar(files: dict[str, bytes], modes: dict[str, int] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dirs = sorted({str(Path(n).parent) for n in files} - {"."})
        for d in ["."] + [f"./{d}" for d in dirs]:
            info = tarfile.TarInfo(d)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(f"./{name}")
            info.size, info.mode = len(data), (modes or {}).get(name, 0o755)
            tar.addfile(info, io.BytesIO(data))
    return _compress(buf.getvalue())


def build_dir(directory: Path, ctx: dict[str, Any], files: dict[str, bytes] | None = None, **tar_kw: Any) -> str:
    """The release-<job_key> artifact as build-on-hpc hands it over; the archive's digest."""
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / ctx["filename"]
    archive.write_bytes(_tar(files or {"bin/pkg-version": b"#!/bin/sh\necho 2.5.0\n"}, **tar_kw))
    digest = release_tree.sha256_file(archive)
    (directory / "SHA256SUMS").write_text(f"{digest}  {ctx['filename']}\n")
    itest = {"result": "passed", "sha256": digest, "job_key": ctx["job_key"], "sha": ctx["sha"]}
    (directory / f"itest-{ctx['leg_id']}.json").write_text(json.dumps(itest))
    (directory / "build-meta.json").write_text(json.dumps({"release": {"job_key": ctx["job_key"]}}))
    (directory / "build.log").write_text("the job's log\n")
    return digest


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
    for k, v in {"GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "o/pkg"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    leg = hpc_leg(tmp_path)
    ctx = leg["_release"]
    return {
        "leg": leg,
        "ctx": ctx,
        "prefix": Path(ctx["prefix"]),
        "locks": tmp_path / "locks",
        "build": tmp_path / "release",
        "receipts": tmp_path / "receipts",
    }


def _promote(s: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return promote.run_promote(
        LocalConn(),
        matrix_leg=json.dumps(s["leg"]),
        archive_dir=s["build"],
        lock_root=str(s["locks"]),
        receipt_dir=s["receipts"],
        **kw,
    )


def _writable(root: Path) -> list[str]:
    return [str(p) for p in [root, *root.rglob("*")] if p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)]


def _make_writable(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        p.chmod(p.stat().st_mode | stat.S_IWUSR)


@pytest.fixture(autouse=True)
def _cleanup_read_only(tmp_path: Path) -> Any:
    yield
    if tmp_path.exists():
        _make_writable(tmp_path)


def test_promote_installs_the_tested_bytes_read_only(setup: dict[str, Any]) -> None:
    digest = build_dir(setup["build"], setup["ctx"])
    receipt = _promote(setup)
    prefix: Path = setup["prefix"]
    assert (receipt["action"], receipt["sha256"], receipt["prefix"]) == ("promoted", digest, str(prefix))
    assert (prefix / "bin" / "pkg-version").read_text() == "#!/bin/sh\necho 2.5.0\n"
    installed = json.loads((prefix / ".release.json").read_text())
    assert installed["sha256"] == digest and installed["job_key"] == setup["ctx"]["job_key"]
    assert installed["run"]["run_url"] == "https://github.com/o/pkg/actions/runs/77"
    assert (prefix / "build.log").read_text() == "the job's log\n"
    assert _writable(prefix) == []
    # Nothing left behind: no incoming dir, lock or staged archive.
    assert not list(prefix.parent.glob(".*incoming*"))
    assert not (setup["locks"] / "promote" / "pkg").exists()
    assert not list((setup["locks"] / "staging").iterdir())
    written = json.loads((setup["receipts"] / f"receipt-{setup['ctx']['leg_id']}.json").read_text())
    assert written == receipt
    assert f"receipt={setup['receipts']}/receipt-{setup['ctx']['leg_id']}.json" in (
        Path(os.environ["GITHUB_OUTPUT"]).read_text()
    )


def test_promoting_the_same_bytes_again_is_a_no_op(setup: dict[str, Any]) -> None:
    build_dir(setup["build"], setup["ctx"])
    _promote(setup)
    before = (setup["prefix"] / ".release.json").read_text()
    assert _promote(setup)["action"] == "already-present"
    assert (setup["prefix"] / ".release.json").read_text() == before


def test_other_bytes_of_the_same_release_are_refused(setup: dict[str, Any]) -> None:
    """A rebuild is never byte-identical (tar mtimes): an installed tree is never replaced."""
    build_dir(setup["build"], setup["ctx"])
    _promote(setup)
    shutil.rmtree(setup["build"])
    build_dir(setup["build"], setup["ctx"], {"bin/pkg-version": b"rebuilt\n"})
    with pytest.raises(CIError, match="from other bytes .*never replaced"):
        _promote(setup)
    assert (setup["prefix"] / "bin" / "pkg-version").read_text() == "#!/bin/sh\necho 2.5.0\n"


def test_another_release_at_the_prefix_is_refused(setup: dict[str, Any]) -> None:
    prefix: Path = setup["prefix"]
    prefix.mkdir(parents=True)
    (prefix / ".release.json").write_text(json.dumps({**release_tree.identity(setup["ctx"]), "sha": "f" * 40}))
    build_dir(setup["build"], setup["ctx"])
    with pytest.raises(CIError, match="holds another release"):
        _promote(setup)


def test_a_build_skipped_as_published_is_only_checked(setup: dict[str, Any]) -> None:
    build_dir(setup["build"], setup["ctx"])
    digest = _promote(setup)["sha256"]
    shutil.rmtree(setup["build"])
    installed = json.loads((setup["prefix"] / ".release.json").read_text())
    release_tree.write_already_published(setup["build"], installed)
    receipt = _promote(setup)
    assert (receipt["action"], receipt["sha256"]) == ("already-published", digest)


def test_a_build_skipped_as_published_refuses_a_prefix_changed_since(setup: dict[str, Any]) -> None:
    build_dir(setup["build"], setup["ctx"])
    _promote(setup)
    shutil.rmtree(setup["build"])
    installed = json.loads((setup["prefix"] / ".release.json").read_text())
    release_tree.write_already_published(setup["build"], {**installed, "sha256": "e" * 64})
    with pytest.raises(CIError, match="changed since the build job looked"):
        _promote(setup)


def test_a_build_skipped_as_published_needs_the_tree(setup: dict[str, Any]) -> None:
    release_tree.write_already_published(setup["build"], {**release_tree.identity(setup["ctx"]), "sha256": "d" * 64})
    with pytest.raises(CIError, match="is empty now: re-run all"):
        _promote(setup)


def test_what_an_interrupted_promote_left_is_removed(setup: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    prefix: Path = setup["prefix"]
    leftover = prefix.parent / f".{prefix.name}.incoming.66-1"
    (leftover / "bin").mkdir(parents=True)
    (leftover / "bin" / "half").write_text("x")
    for p in (leftover / "bin" / "half", leftover / "bin", leftover):
        p.chmod(0o555)
    build_dir(setup["build"], setup["ctx"])
    assert _promote(setup)["action"] == "promoted"
    assert not leftover.exists()
    assert "removing what an interrupted promote left" in capsys.readouterr().out


def test_the_kill_switch_stops_promote(setup: dict[str, Any]) -> None:
    setup["locks"].mkdir(parents=True)
    (setup["locks"] / "DISABLED").write_text("incident 42\n")
    build_dir(setup["build"], setup["ctx"])
    with pytest.raises(CIError, match="CD is disabled on this HPC: .*incident 42"):
        _promote(setup)
    assert not setup["prefix"].exists()


def _hand_made_lock(lock: Path, *, age_minutes: float) -> None:
    lock.mkdir(parents=True)
    (lock / "owner").write_text('{"run_id":"1-1","run_url":"https://github.com/o/pkg/actions/runs/1"}\n')
    old = time.time() - age_minutes * 60
    os.utime(lock / "owner", (old, old))
    os.utime(lock, (old, old))


def test_s3a_a_held_promote_lock_times_out_naming_its_holder(
    setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    _hand_made_lock(setup["locks"] / "promote" / "pkg", age_minutes=0)
    build_dir(setup["build"], setup["ctx"])
    with pytest.raises(CIError, match="waiting for the promote lock .*actions/runs/1"):
        _promote(setup, timeout=0)
    assert not setup["prefix"].exists()


def test_s3b_a_stale_promote_lock_is_broken(setup: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    _hand_made_lock(setup["locks"] / "promote" / "pkg", age_minutes=10)
    build_dir(setup["build"], setup["ctx"])
    assert _promote(setup, stale_minutes=5)["action"] == "promoted"
    assert "broke the stale lock" in capsys.readouterr().out


def test_an_unsafe_archive_is_refused_before_the_cluster_is_touched(setup: dict[str, Any]) -> None:
    build_dir(setup["build"], setup["ctx"], {"bin/su": b"x"}, modes={"bin/su": 0o4755})
    with pytest.raises(CIError, match="setuid"):
        _promote(setup)
    assert not setup["locks"].exists() and not setup["prefix"].exists()


def test_a_tampered_build_is_refused(setup: dict[str, Any]) -> None:
    build_dir(setup["build"], setup["ctx"])
    (setup["build"] / setup["ctx"]["filename"]).write_bytes(_tar({"bin/evil": b"x"}))
    with pytest.raises(CIError, match="is not the .* the build wrote"):
        _promote(setup)


def test_a_rehearsal_is_never_promoted(setup: dict[str, Any]) -> None:
    setup["leg"]["_release"] = {**setup["ctx"], "mode": "rehearsal"}
    with pytest.raises(CIError, match="never promoted"):
        _promote(setup)


def test_the_production_tree_is_refused(setup: dict[str, Any]) -> None:
    setup["leg"]["_release"] = {**setup["ctx"], "prefix": "/usr/local/apps/pkg/2.5.0-rc1/GNU/8.5"}
    build_dir(setup["build"], setup["leg"]["_release"])
    with pytest.raises(CIError, match="allow-production"):
        _promote(setup)


def test_the_default_lock_root_is_in_home(
    setup: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    build_dir(setup["build"], setup["ctx"])
    seen: list[str] = []
    real = transfer.remote_lock

    def spy(conn: Any, **kw: Any) -> Any:
        seen.append(kw["lock_dir"])
        return real(conn, **kw)

    monkeypatch.setattr(transfer, "remote_lock", spy)
    promote.run_promote(
        LocalConn(),
        matrix_leg=json.dumps(setup["leg"]),
        archive_dir=setup["build"],
        lock_root=promote.DEFAULT_LOCK_ROOT,
        receipt_dir=setup["receipts"],
    )
    assert seen == [f"{tmp_path}/home/.cd-locks/promote/pkg"]


@pytest.mark.parametrize("raw", ["", "~", "~/locks", "$HOME/.cd-locks", "/abs/locks", "$SCRATCH/l"])
def test_lock_roots(raw: str) -> None:
    assert promote.check_lock_root(raw).startswith(("$HOME", "/", "$SCRATCH"))


@pytest.mark.parametrize("raw", ["relative", "$HOME/../x", "$FOO/x", "/a b", "/x;rm"])
def test_bad_lock_roots(raw: str) -> None:
    with pytest.raises(CIError, match="lock root"):
        promote.check_lock_root(raw)


@pytest.mark.parametrize("name", [".release.json", "build.log"])
def test_an_archive_carrying_a_sidecar_name_is_refused(setup: dict[str, Any], name: str) -> None:
    """promote writes these two beside the tree; the archive may not put anything there first."""
    build_dir(setup["build"], setup["ctx"], {name: b"{}", "bin/x": b"x"})
    with pytest.raises(CIError, match=f"'./{name}': a reserved name"):
        _promote(setup)
    assert not setup["locks"].exists() and not setup["prefix"].exists()


def test_a_sidecar_is_never_written_through_a_link(tmp_path: Path) -> None:
    """Belt and braces behind the archive check: a link (even dangling) at the sidecar path stops promote."""
    incoming, outside = tmp_path / "incoming", tmp_path / "outside"
    incoming.mkdir()
    (incoming / "build.log").symlink_to(outside)
    src = tmp_path / "log"
    src.write_text("the job's log\n")
    (tmp_path / "staging").mkdir()
    staged = tmp_path / "staging" / "k.build.log"
    with pytest.raises(CIError, match="exists already: the archive carries it"):
        promote._place_sidecar(LocalConn(), src, staged=str(staged), dest=str(incoming / "build.log"))
    assert not outside.exists() and not staged.exists()
    promote._place_sidecar(LocalConn(), src, staged=str(staged), dest=str(incoming / "other.log"))
    assert (incoming / "other.log").read_text() == "the job's log\n" and not staged.exists()
    (incoming / "build.log").unlink()  # dangling: the read-only cleanup would trip on it


def test_a_symlinked_release_json_is_never_read(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (tmp_path / "elsewhere.json").write_text("{}")
    (prefix / ".release.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(CIError, match="is a symlink"):
        release_tree.read_release_json(LocalConn(), str(prefix))


class _FailingConn(LocalConn):
    """ssh itself fails (255) for every command, as when the cluster is unreachable."""

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> Any:
        return subprocess.Popen(
            ["bash", "-c", "echo 'ssh: connect to host hpc-login: Connection timed out' >&2; exit 255"],
            stdout=stdout,
            stderr=stderr,
        )


def test_an_unreadable_kill_switch_stops_promote(tmp_path: Path) -> None:
    """Exit 255 is not proof that DISABLED is absent: stop, naming why."""
    with pytest.raises(CIError, match=r"Cannot tell whether CD is disabled .*exit 255.*Connection timed out"):
        promote.check_kill_switch(_FailingConn(), str(tmp_path / "locks"))


def test_the_kill_switch_passes_only_on_a_clean_absent(tmp_path: Path) -> None:
    promote.check_kill_switch(LocalConn(), str(tmp_path / "locks"))
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / "DISABLED").symlink_to(tmp_path / "nowhere")
    with pytest.raises(CIError, match="CD is disabled on this HPC"):
        promote.check_kill_switch(LocalConn(), str(tmp_path / "locks"))
    (tmp_path / "locks" / "DISABLED").unlink()  # dangling: the read-only cleanup would trip on it
