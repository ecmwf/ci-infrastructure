# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Runner<->cluster transfers, run against the local filesystem."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import FakeProc

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript, transfer
from ci_infrastructure.hpc.site import ensure_batch_site, load_site


class FakeConnection:
    """Records execute/sendfile/getfile calls; execute returns a canned proc."""

    def __init__(self, exec_returncode: int = 0, exec_stderr: bytes = b"") -> None:
        self.executed: list[list[str]] = []
        self.sent: list[tuple[str, str]] = []
        self.fetched: list[tuple[str, str]] = []
        self._exec_returncode = exec_returncode
        self._exec_stderr = exec_stderr

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        self.executed.append([str(c) for c in command])
        return FakeProc(returncode=self._exec_returncode, stderr=self._exec_stderr)

    def sendfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        self.sent.append((str(src), str(dst)))

    def getfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        self.fetched.append((str(src), str(dst)))


class CopyingConnection(FakeConnection):
    """Really copies files and runs remote commands locally, failing on a non-zero exit."""

    def sendfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        super().sendfile(src, dst)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(Path(src).read_bytes())

    def getfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        super().getfile(src, dst)
        Path(dst).write_bytes(Path(src).read_bytes())

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        proc = super().execute(command, stdout, stderr, dryrun)
        argv = [str(c) for c in command]
        if argv[:2] == ["bash", "-c"]:
            subprocess.run(argv[2], shell=True, check=True)
        else:
            subprocess.run(argv, check=True)
        return proc


class ShellConnection(FakeConnection):
    """Runs remote commands locally and reports the exit code instead of raising."""

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        argv = [str(c) for c in command]
        self.executed.append(argv)
        proc = subprocess.run(["bash", "-c", argv[2]] if argv[:2] == ["bash", "-c"] else argv)
        return FakeProc(returncode=proc.returncode)


class StderrShellConnection(FakeConnection):
    """Runs remote commands locally and hands back their stderr."""

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        proc = subprocess.run([str(c) for c in command], capture_output=True)
        return FakeProc(returncode=proc.returncode, stderr=proc.stderr)


class LockConnection(FakeConnection):
    """The lock `mkdir` fails `busy_for` times, then succeeds."""

    def __init__(self, busy_for: int) -> None:
        super().__init__()
        self.busy_for = busy_for
        self.attempts = 0

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        argv = [str(c) for c in command]
        self.executed.append(argv)
        if argv[:2] == ["bash", "-c"] and "mkdir" in argv[2] and transfer.SHIP_LOCK_SUFFIX in argv[2]:
            self.attempts += 1
            if self.attempts <= self.busy_for:
                return FakeProc(returncode=1)
        return FakeProc()


def _write_zstd_tar(archive: Path, tree: Path) -> None:
    """Stand in for the job's final step: a .tar.zst of `tree`."""
    plain = archive.with_suffix(".plain.tar")
    with tarfile.open(plain, "w") as tar:
        tar.add(tree, arcname=".")
    subprocess.run(["zstd", "-q", "-f", str(plain), "-o", str(archive)], check=True)
    plain.unlink()


def _make_tree(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body)
    return root


def test_ship_source_ships_and_unpacks_dep_prefixes(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    dep0 = _make_tree(tmp_path / "dep0", "libfoo.a", "x")
    dep1 = _make_tree(tmp_path / "dep1", "libbar.a", "y")
    staging = tmp_path / "remote" / "staging" / "art"

    transfer.ship_source(
        CopyingConnection(),
        local_source_dir=str(src),
        staging_dir=str(staging),
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
        local_prefixes=[str(dep0), str(dep1)],
        remote_deps_dir=str(staging / "deps"),
    )

    assert (staging / "deps" / "dep0" / "libfoo.a").read_text() == "x"
    assert (staging / "deps" / "dep1" / "libbar.a").read_text() == "y"
    assert (staging / "TRANSFER_COMPLETED").is_file()


def test_ship_source_reports_each_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    dep = _make_tree(tmp_path / "eckit", "libeckit.a", "x")
    staging = tmp_path / "remote" / "staging" / "art"

    transfer.ship_source(
        CopyingConnection(),
        local_source_dir=str(src),
        staging_dir=str(staging),
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
        local_prefixes=[str(dep)],
        remote_deps_dir=str(staging / "deps"),
    )

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("ship: source (1 files, ") and "staging reset" in out[0] and "send" in out[0]
    assert out[1].startswith("ship: eckit (1 files, ") and out[1].endswith("s") and "unpack" in out[1]
    assert out[2].startswith("ship: done in ")


def test_ship_source_raises_on_remote_failure(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    conn = FakeConnection(exec_returncode=1, exec_stderr=b"permission denied")
    with pytest.raises(CIError, match="Staging reset failed.*permission denied"):
        transfer.ship_source(
            conn,
            local_source_dir=str(src),
            staging_dir="/remote/staging/art",
            run_id="42-1",
            tar_dir=str(tmp_path / "stage"),
        )


def test_ship_source_reset_clears_prepopulated_staging(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    staging = tmp_path / "remote" / "staging" / "art"
    (staging / "deps" / "0").mkdir(parents=True)
    (staging / "deps" / "0" / "wheel.whl").write_text("stale")
    (staging / "TRANSFER_COMPLETED_00-0").write_text("")

    transfer.ship_source(
        CopyingConnection(),
        local_source_dir=str(src),
        staging_dir=str(staging),
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
    )

    assert not (staging / "TRANSFER_COMPLETED_00-0").exists()
    assert not (staging / "deps").exists()
    assert not (staging.parent / "art.trash.42-1").exists()
    assert (staging / "source.tgz").is_file()
    assert (staging / "TRANSFER_COMPLETED").is_file()


def test_truncate_remote_file_empties_a_previous_attempts_output(tmp_path: Path) -> None:
    output = tmp_path / "hpc-jobs" / "art.out"
    output.parent.mkdir(parents=True)
    output.write_text(f"building...\n{jobscript.SENTINEL_FAILURE} 1111\n")
    transfer.truncate_remote_file(CopyingConnection(), path=str(output))
    assert output.read_text() == ""


def test_truncate_remote_file_creates_the_output_and_its_parent(tmp_path: Path) -> None:
    output = tmp_path / "hpc-jobs" / "art.out"
    transfer.truncate_remote_file(CopyingConnection(), path=str(output))
    assert output.is_file()


def test_ship_lock_creates_the_lock_parent_before_claiming(tmp_path: Path) -> None:
    """A first-ever build has no `staging/` yet, and the claim `mkdir` is not `-p`."""
    lock = tmp_path / "work" / "staging" / ("art" + transfer.SHIP_LOCK_SUFFIX)
    with transfer.ship_lock(ShellConnection(), staging_dir=str(tmp_path / "work" / "staging" / "art"), run_id="1-1"):
        assert lock.is_dir()
    assert not lock.exists()


def test_ship_lock_is_exclusive_against_a_second_shipper(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "art"
    conn = ShellConnection()
    lock_dir = transfer._ship_lock_path(str(staging))
    with transfer.ship_lock(conn, staging_dir=str(staging), run_id="1-1"):
        assert not transfer._try_acquire_lock(conn, lock_dir=lock_dir, run_id="2-1", stale_minutes=30)
    assert transfer._try_acquire_lock(conn, lock_dir=lock_dir, run_id="2-1", stale_minutes=30)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the read-only parent")
def test_a_lock_that_cannot_be_created_fails_at_once(tmp_path: Path) -> None:
    """Not "locked by another run" for 15 minutes: the filesystem refused, so say why."""
    parent = tmp_path / "locks"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(CIError, match="Cannot create the lock .*Permission denied.*No other run holds it"):
            transfer._try_acquire_lock(
                StderrShellConnection(), lock_dir=str(parent / "art"), run_id="1-1", stale_minutes=30
            )
    finally:
        parent.chmod(0o700)


def test_ship_lock_waits_for_the_holder_then_acquires(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    conn = LockConnection(busy_for=2)
    with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1", poll=7):
        pass
    assert conn.attempts == 3
    assert slept == [7, 7]


def test_ship_lock_times_out_rather_than_shipping_anyway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    conn = LockConnection(busy_for=10_000)
    with pytest.raises(CIError, match="waiting for the staging lock"):
        with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1", timeout=0, poll=1):
            raise AssertionError("body must not run without the lock")


def test_ship_lock_releases_when_the_ship_fails(tmp_path: Path) -> None:
    staging = tmp_path / "staging" / "art"
    with pytest.raises(CIError, match="boom"):
        with transfer.ship_lock(ShellConnection(), staging_dir=str(staging), run_id="1-1"):
            raise CIError("boom")
    assert not Path(transfer._ship_lock_path(str(staging))).exists()


class CapturingShellConnection(FakeConnection):
    """Runs remote commands locally and hands back their stdout, stderr and exit code."""

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        argv = [str(c) for c in command]
        self.executed.append(argv)
        proc = subprocess.run(argv, capture_output=True)
        return FakeProc(stdout=proc.stdout, returncode=proc.returncode, stderr=proc.stderr)


def _hand_made_lock(lock: Path, owner: str, *, age_minutes: float) -> None:
    """A lock as a run that died while holding it left it: an owner file and an old mtime."""
    lock.mkdir(parents=True)
    (lock / transfer.LOCK_OWNER_FILE).write_text(owner + "\n")
    old = time.time() - age_minutes * 60
    os.utime(lock / transfer.LOCK_OWNER_FILE, (old, old))
    os.utime(lock, (old, old))


def test_the_owner_file_names_the_claim_as_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {"GITHUB_SERVER_URL": "https://gh", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7"}.items():
        monkeypatch.setenv(k, v)
    lock = tmp_path / "locks" / "promote" / "pkg"
    with transfer.remote_lock(
        CapturingShellConnection(), lock_dir=str(lock), run_id="7-1", what="promote", subject="x"
    ):
        owner = json.loads((lock / transfer.LOCK_OWNER_FILE).read_text())
    assert owner["run_id"] == "7-1"
    assert owner["run_url"] == "https://gh/o/r/actions/runs/7"
    assert {"host", "pid", "acquired_at", "repository", "job"} <= set(owner)
    assert not lock.exists()


def test_two_claims_of_one_run_have_different_owners() -> None:
    """A re-run attempt must never take an earlier claim of the same run id for its own."""
    assert transfer.lock_owner("7-1") != transfer.lock_owner("7-1")


def test_a_lock_that_is_no_longer_ours_is_not_released(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lock = tmp_path / "locks" / "modulemgr"
    other = '{"run_id":"9-1","host":"elsewhere"}'
    with transfer.remote_lock(CapturingShellConnection(), lock_dir=str(lock), run_id="7-1", what="sync", subject="x"):
        # Broken as stale and claimed by another run meanwhile.
        (lock / transfer.LOCK_OWNER_FILE).write_text(other + "\n")
    assert (lock / transfer.LOCK_OWNER_FILE).read_text() == other + "\n"
    assert "not releasing the sync lock" in capsys.readouterr().out


def test_a_stale_lock_of_a_finished_run_is_broken_and_its_owner_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """S3(b): a lock left by a run that is gone, made by hand with an old mtime, as on the cluster."""
    lock = tmp_path / "locks" / "promote" / "pkg"
    dead = '{"run_id":"1-1","run_url":"https://github.com/o/r/actions/runs/1"}'
    _hand_made_lock(lock, dead, age_minutes=10)
    conn = CapturingShellConnection()
    with transfer.remote_lock(conn, lock_dir=str(lock), run_id="2-1", what="promote", subject="x", stale_minutes=5):
        assert json.loads((lock / transfer.LOCK_OWNER_FILE).read_text())["run_id"] == "2-1"
    out = capsys.readouterr().out
    assert "broke the stale lock" in out and "actions/runs/1" in out
    assert not lock.exists()
    assert not list(lock.parent.glob("pkg.stale.*"))


def test_a_fresh_lock_is_waited_for_and_the_timeout_names_its_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S3(a): a lock held by a live run (fresh mtime) is never broken; the waiter times out."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    lock = tmp_path / "locks" / "promote" / "pkg"
    holder = '{"run_id":"1-1","run_url":"https://github.com/o/r/actions/runs/1"}'
    _hand_made_lock(lock, holder, age_minutes=0)
    with pytest.raises(CIError, match=r"Timed out after 0s waiting for the promote lock .*actions/runs/1"):
        with transfer.remote_lock(
            CapturingShellConnection(), lock_dir=str(lock), run_id="2-1", what="promote", subject="x", timeout=0
        ):
            raise AssertionError("body must not run without the lock")
    assert (lock / transfer.LOCK_OWNER_FILE).read_text() == holder + "\n"


def test_the_heartbeat_keeps_a_held_lock_fresh(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "modulemgr"
    with transfer.remote_lock(
        CapturingShellConnection(), lock_dir=str(lock), run_id="7-1", what="sync", subject="x", heartbeat=0.05
    ):
        old = time.time() - 3600
        os.utime(lock, (old, old))
        deadline = time.time() + 10
        while lock.stat().st_mtime < time.time() - 60 and time.time() < deadline:
            time.sleep(0.05)
        assert lock.stat().st_mtime > time.time() - 60
    assert not lock.exists()


def test_dryrun_transfers_nothing_and_writes_no_local_tarball(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    stage = str(tmp_path / "stage")
    conn = FakeConnection()
    transfer.ship_source(
        conn, local_source_dir=str(src), staging_dir="/r/staging/art", run_id="1-1", tar_dir=stage, dryrun=True
    )
    with transfer.ship_lock(conn, staging_dir="/r/staging/art", run_id="1-1", dryrun=True):
        pass
    transfer.fetch_tree(conn, remote_dir="/r/ref/art", local_dir=str(tmp_path / "out"), tar_dir=stage, dryrun=True)
    transfer.push_tree(conn, local_dir=str(src), remote_dir="/r/inputs/art", tar_dir=stage, dryrun=True)
    transfer.remove_tree(conn, remote_dir="/r/out/art", dryrun=True)
    assert conn.executed == [] and conn.sent == [] and conn.fetched == []
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "out").exists()


def test_push_then_fetch_roundtrip_preserves_tree(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "inputs", "hello.txt", "content-xyz")
    conn = CopyingConnection()
    remote = tmp_path / "remote" / "inputs" / "art"
    transfer.push_tree(conn, local_dir=str(src), remote_dir=str(remote), tar_dir=str(tmp_path / "stage"))
    assert (remote / "hello.txt").read_text() == "content-xyz"
    assert not any((tmp_path / "stage").iterdir())

    fetched = tmp_path / "back"
    transfer.fetch_tree(conn, remote_dir=str(remote), local_dir=str(fetched), tar_dir=str(tmp_path / "stage2"))
    assert (fetched / "hello.txt").read_text() == "content-xyz"
    assert not any((tmp_path / "stage2").iterdir())

    transfer.remove_tree(conn, remote_dir=str(remote))
    assert not remote.exists()


@pytest.mark.skipif(sys.version_info < (3, 11, 4), reason="tarfile extraction filters (PEP 706)")
def test_ship_then_fetch_roundtrip_preserves_tree(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "hello.txt", "content-xyz")
    conn = CopyingConnection()
    staging = tmp_path / "remote" / "staging" / "art"
    transfer.ship_source(
        conn, local_source_dir=str(src), staging_dir=str(staging), run_id="42-1", tar_dir=str(tmp_path / "stage")
    )
    assert transfer.marker_exists(ShellConnection(), staging_dir=str(staging))
    assert not transfer.marker_exists(ShellConnection(), staging_dir=str(tmp_path))
    assert not any((tmp_path / "stage").iterdir())
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(staging / "source.tgz") as tar:
        tar.extractall(unpacked, filter="data")
    assert (unpacked / "hello.txt").read_text() == "content-xyz"

    _write_zstd_tar(Path(jobscript.install_archive_path(str(unpacked))), unpacked)
    fetched = tmp_path / "back"
    transfer.fetch_install(
        conn, remote_install_dir=str(unpacked), local_install_dir=str(fetched), tar_dir=str(tmp_path / "stage2")
    )
    assert (fetched / "hello.txt").read_text() == "content-xyz"
    assert not any((tmp_path / "stage2").iterdir())


def test_fetch_install_fails_when_the_job_wrote_no_archive(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        transfer.fetch_install(
            CopyingConnection(),
            remote_install_dir=str(tmp_path / "never-archived"),
            local_install_dir=str(tmp_path / "back"),
            tar_dir=str(tmp_path / "stage"),
        )


# Kept in step with ecmwf/build-package-hpc's config.yml.
BATCH_SITES: Final = ["hpc-batch", "aa-batch", "ab-batch", "ac-batch", "ad-batch", "ag-batch", "lumi"]
DIRECT_SITES: Final = ["hpc-login", "lumi-login", "local-direct"]


@pytest.mark.parametrize("site_name", BATCH_SITES)
def test_packaged_config_provides_batch_site(site_name: str) -> None:
    ensure_batch_site(load_site(site_name), site_name)


@pytest.mark.parametrize("site_name", DIRECT_SITES)
def test_packaged_config_rejects_direct_site_for_builds(site_name: str) -> None:
    with pytest.raises(CIError):
        ensure_batch_site(load_site(site_name), site_name)


def test_packaged_config_rejects_an_unknown_site() -> None:
    with pytest.raises(Exception, match="Unknown site"):
        load_site("no-such-cluster")


class InterleavingShellConnection(CapturingShellConnection):
    """Runs the acquire script with another waiter's moves spliced in at one of its seams."""

    def __init__(self, at: dict[str, str]) -> None:
        super().__init__()
        self.at = at

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        argv = [str(c) for c in command]
        if argv[:2] == ["bash", "-c"]:
            script = argv[2]
            for seam, moves in self.at.items():
                script = script.replace(seam, moves)
            argv = [*argv[:2], script]
        return super().execute(argv, stdout, stderr, dryrun)


def _reclaim(lock: Path, owner: str) -> str:
    """Shell: another waiter breaks the lock and claims it afresh."""
    return f"rm -rf '{lock}'; mkdir '{lock}'; printf '%s\\n' '{owner}' > '{lock}/{transfer.LOCK_OWNER_FILE}'"


def _owner(lock: Path) -> str:
    return (lock / transfer.LOCK_OWNER_FILE).read_text().strip()


DEAD: Final = '{"run_id":"1-1"}'
FRESH: Final = '{"run_id":"2-1"}'


def test_a_waiter_paused_after_its_stale_look_leaves_a_fresh_reclaim_alone(tmp_path: Path) -> None:
    """Waiter A saw the lock stale, then B broke and re-claimed it before A went on."""
    lock = tmp_path / "locks" / "pkg"
    _hand_made_lock(lock, DEAD, age_minutes=10)
    conn = InterleavingShellConnection({transfer.LOCK_SEAM_SEEN: _reclaim(lock, FRESH)})
    assert not transfer._try_acquire_lock(conn, lock_dir=str(lock), run_id="3-1", stale_minutes=5)
    assert _owner(lock) == FRESH
    assert sorted(p.name for p in lock.parent.iterdir()) == ["pkg"]  # no .stale, no .breaking left


def test_a_reclaim_between_recheck_and_rename_is_given_back(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "pkg"
    _hand_made_lock(lock, DEAD, age_minutes=10)
    conn = InterleavingShellConnection({transfer.LOCK_SEAM_RECHECKED: _reclaim(lock, FRESH)})
    assert not transfer._try_acquire_lock(conn, lock_dir=str(lock), run_id="3-1", stale_minutes=5)
    assert _owner(lock) == FRESH
    assert sorted(p.name for p in lock.parent.iterdir()) == ["pkg"]


def test_a_renamed_fresh_lock_is_never_deleted_when_the_path_is_claimed_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex 2's three waiters: B re-claims, A renames B's lock aside, C claims the vacant path."""
    lock = tmp_path / "locks" / "pkg"
    third = '{"run_id":"4-1"}'
    _hand_made_lock(lock, DEAD, age_minutes=10)
    conn = InterleavingShellConnection(
        {
            transfer.LOCK_SEAM_RECHECKED: _reclaim(lock, FRESH),
            transfer.LOCK_SEAM_RENAMED: f"mkdir '{lock}'; echo '{third}' > '{lock}/{transfer.LOCK_OWNER_FILE}'",
        }
    )
    assert not transfer._try_acquire_lock(conn, lock_dir=str(lock), run_id="3-1", stale_minutes=5)
    assert _owner(lock) == third
    [aside] = lock.parent.glob("pkg.stale.*")
    assert _owner(aside) == FRESH  # B's lock kept, not rm -rf'd
    assert "left" in capsys.readouterr().out and not (lock.parent / "pkg.breaking").exists()


def test_a_held_break_sublock_makes_the_waiter_retry(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "pkg"
    _hand_made_lock(lock, DEAD, age_minutes=10)
    (lock.parent / "pkg.breaking").mkdir()
    assert not transfer._try_acquire_lock(CapturingShellConnection(), lock_dir=str(lock), run_id="3-1", stale_minutes=5)
    assert _owner(lock) == DEAD and (lock.parent / "pkg.breaking").is_dir()


def test_a_dead_breakers_sublock_is_cleared_by_the_stale_rule(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "pkg"
    _hand_made_lock(lock, DEAD, age_minutes=10)
    breaking = lock.parent / "pkg.breaking"
    breaking.mkdir()
    old = time.time() - 600
    os.utime(breaking, (old, old))
    assert transfer._try_acquire_lock(CapturingShellConnection(), lock_dir=str(lock), run_id="3-1", stale_minutes=5)
    assert json.loads(_owner(lock))["run_id"] == "3-1"
    assert sorted(p.name for p in lock.parent.iterdir()) == ["pkg"]
