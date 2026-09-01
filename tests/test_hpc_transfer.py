# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the runner<->cluster tree transfers.

These drive ship_source / fetch_install against a recording fake connection and
a real local tar, so they exercise the command sequence and the tar round-trip
with no ssh and no cluster.
"""

from __future__ import annotations

import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Final

import pytest

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript, transfer
from ci_infrastructure.hpc.site import ensure_batch_site, load_site


class FakeProc:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._stderr


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
        return FakeProc(self._exec_returncode, self._exec_stderr)

    def sendfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        self.sent.append((str(src), str(dst)))

    def getfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        self.fetched.append((str(src), str(dst)))


class TarballConnection(FakeConnection):
    """A connection whose ``getfile`` delivers a real (empty) tarball.

    fetch_tree/fetch_install really untar what arrived, so a no-op getfile would
    fail the extract rather than exercise it.
    """

    def getfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        super().getfile(src, dst)
        with tarfile.open(dst, "w:gz"):
            pass


class CopyingConnection(FakeConnection):
    """A connection that really moves bytes and really runs the remote commands.

    ``sendfile``/``getfile`` copy the file and ``execute`` runs the argv locally,
    so a transfer can be followed end to end (tar -> ship -> untar) instead of
    only asserting the command sequence.
    """

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
            subprocess.run(argv, check=True)  # rm -rf / mkdir -p / touch
        return proc


def _make_tree(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body)
    return root


# --------------------------------------------------------------------------- #
# ship_source (submit-then-poll: tar -> scp tarball -> touch marker)
# --------------------------------------------------------------------------- #
def test_ship_source_clears_stages_and_marks(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    conn = FakeConnection()

    transfer.ship_source(
        conn,
        local_source_dir=str(src),
        staging_dir="/remote/staging/art",
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
    )

    # A local tarball was produced and scp'd into the staging dir under the fixed name.
    assert conn.sent == [(str(tmp_path / "stage" / "42-1.src.tgz"), "/remote/staging/art/source.tgz")]
    # Remote: reset the staging tree via an atomic rename-aside rather than a bare
    # `rm -rf staging_dir` (which races a concurrent shipper / sibling job reading
    # <staging>/deps and fails with ENOTEMPTY). The old tree is moved to a per-run
    # trash name, deleted best-effort, and the dir recreated; the marker drops LAST.
    assert conn.executed[0][:2] == ["bash", "-c"]
    reset = conn.executed[0][2]
    assert "mv /remote/staging/art /remote/staging/art.trash.42-1" in reset
    assert "rm -rf /remote/staging/art.trash.42-1" in reset
    assert reset.rstrip().endswith("mkdir -p /remote/staging/art")
    assert conn.executed[1] == ["touch", "/remote/staging/art/TRANSFER_COMPLETED"]


def test_ship_source_ships_and_unpacks_dep_prefixes_before_the_marker(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    dep0 = _make_tree(tmp_path / "dep0", "libfoo.a", "x")
    dep1 = _make_tree(tmp_path / "dep1", "libbar.a", "y")
    conn = FakeConnection()

    transfer.ship_source(
        conn,
        local_source_dir=str(src),
        staging_dir="/remote/staging/art",
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
        local_prefixes=[str(dep0), str(dep1)],
        remote_deps_dir="/remote/staging/art/deps",
    )

    # Source tarball plus one tarball per dep prefix were scp'd to <deps>/<i>.tgz.
    assert (str(tmp_path / "stage" / "42-1.src.tgz"), "/remote/staging/art/source.tgz") in conn.sent
    assert (str(tmp_path / "stage" / "42-1.dep0.tgz"), "/remote/staging/art/deps/0.tgz") in conn.sent
    assert (str(tmp_path / "stage" / "42-1.dep1.tgz"), "/remote/staging/art/deps/1.tgz") in conn.sent
    # Each dep dir is created and its tarball unpacked into it on the cluster.
    assert ["mkdir", "-p", "/remote/staging/art/deps/0"] in conn.executed
    assert ["mkdir", "-p", "/remote/staging/art/deps/1"] in conn.executed
    untars = [c[2] for c in conn.executed if c[:2] == ["bash", "-c"]]
    assert any("tar -xzf" in u and "/remote/staging/art/deps/0" in u for u in untars)
    # Marker LAST, after source and every dep are staged.
    assert conn.executed[-1] == ["touch", "/remote/staging/art/TRANSFER_COMPLETED"]


def test_ship_source_dryrun_does_nothing(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    conn = FakeConnection()
    transfer.ship_source(
        conn,
        local_source_dir=str(src),
        staging_dir="/remote/staging/art",
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
        dryrun=True,
    )
    assert conn.executed == [] and conn.sent == []
    assert not (tmp_path / "stage").exists()  # no local tarball either


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
    """A staging dir left populated by a prior/concurrent shipper — including a
    populated deps subtree and a stale marker — is reset cleanly. Executing the
    reset for real proves the rename-aside empties it (a bare rmdir of a
    concurrently repopulated deps is what raised ENOTEMPTY in the field)."""
    src = _make_tree(tmp_path / "checkout", "file.txt", "hello")
    staging = tmp_path / "remote" / "staging" / "art"
    (staging / "deps" / "0").mkdir(parents=True)
    (staging / "deps" / "0" / "wheel.whl").write_text("stale")
    (staging / "TRANSFER_COMPLETED_00-0").write_text("")

    class RealExecConnection(FakeConnection):
        def sendfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
            super().sendfile(src, dst)
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(Path(src).read_bytes())

        def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
            proc = super().execute(command, stdout, stderr, dryrun)
            argv = [str(c) for c in command]
            subprocess.run(
                argv[2] if argv[:2] == ["bash", "-c"] else argv, shell=argv[:2] == ["bash", "-c"], check=True
            )
            return proc

    transfer.ship_source(
        RealExecConnection(),
        local_source_dir=str(src),
        staging_dir=str(staging),
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
    )

    # The stale marker and old deps tree are gone; only this run's inputs remain.
    assert not (staging / "TRANSFER_COMPLETED_00-0").exists()
    assert not (staging / "deps").exists()
    assert not (staging.parent / "art.trash.42-1").exists()  # moved-aside copy deleted
    assert (staging / "source.tgz").is_file()
    assert (staging / "TRANSFER_COMPLETED").is_file()


# --------------------------------------------------------------------------- #
# marker_exists
# --------------------------------------------------------------------------- #
def test_marker_exists_true_on_zero_exit() -> None:
    conn = FakeConnection(exec_returncode=0)
    assert transfer.marker_exists(conn, staging_dir="/remote/staging/art") is True
    probe = conn.executed[0]
    assert probe[:2] == ["sh", "-c"]
    # Keyed by the staging dir alone — no run id, so a reattaching runner can ask
    # "has anyone finished shipping?" without reading the scheduler's Comment.
    assert "test -f /remote/staging/art/TRANSFER_COMPLETED " in probe[2]


def test_marker_exists_accepts_a_legacy_per_run_marker() -> None:
    """Rollover shim: a job submitted by a pre-fix runner already has its source
    staged under TRANSFER_COMPLETED_<run_id>, and must not be re-shipped over."""
    conn = FakeConnection(exec_returncode=0)
    transfer.marker_exists(conn, staging_dir="/remote/staging/art")
    probe = conn.executed[0][2]
    assert "/remote/staging/art/TRANSFER_COMPLETED_*" in probe
    # The glob must reach the shell UNQUOTED or it matches a file literally
    # named "TRANSFER_COMPLETED_*".
    assert "'/remote/staging/art/TRANSFER_COMPLETED_*'" not in probe


def test_marker_exists_false_on_nonzero_exit() -> None:
    conn = FakeConnection(exec_returncode=1)
    assert transfer.marker_exists(conn, staging_dir="/remote/staging/art") is False


# --------------------------------------------------------------------------- #
# ship_lock (serialising two runs that ship the same artifact)
# --------------------------------------------------------------------------- #
class LockConnection(FakeConnection):
    """A connection whose lock `mkdir` fails ``busy_for`` times, then succeeds.

    Stands in for a peer run that is holding the staging lock and eventually
    releases it. Only the acquire script is answered specially; everything else
    keeps FakeConnection's blanket success.
    """

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


class ShellConnection(FakeConnection):
    """Really runs the remote script and reports its TRUE exit code.

    CopyingConnection asserts success (`check=True`), but a lock claim's non-zero
    exit is an *answer* — "someone else holds it" — not an error, so exercising
    the acquire against a real filesystem needs a connection that reports rather
    than raises.
    """

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> FakeProc:
        argv = [str(c) for c in command]
        self.executed.append(argv)
        proc = subprocess.run(["bash", "-c", argv[2]] if argv[:2] == ["bash", "-c"] else argv)
        return FakeProc(returncode=proc.returncode)


def _lock_scripts(conn: FakeConnection) -> list[str]:
    return [argv[2] for argv in conn.executed if argv[:2] == ["bash", "-c"] and transfer.SHIP_LOCK_SUFFIX in argv[2]]


def test_ship_lock_acquires_and_releases_around_the_body() -> None:
    conn = FakeConnection()
    with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1"):
        during = len(conn.executed)
    scripts = _lock_scripts(conn)
    # The lock is a SIBLING of the staging dir: ship_source renames that dir
    # aside, which would carry a lock living inside it away with the tree.
    assert "\nif mkdir /remote/staging/art.shiplock " in scripts[0]
    assert during == 1  # acquired before the body ran
    assert scripts[-1].startswith("rm -rf /remote/staging/art.shiplock ")


def test_ship_lock_creates_the_lock_parent_before_claiming(tmp_path: Path) -> None:
    """A fresh work dir has no `staging/` yet — `_reset_staging_dir` makes it, but
    that runs inside this lock. The claim `mkdir` is deliberately not `-p` (that
    would succeed on an existing lock and defeat the test-and-set), so without a
    separate parent `mkdir -p` every attempt fails ENOENT and the ship waits out
    its whole timeout on a first-ever build."""
    staging = tmp_path / "work" / "staging" / "art"
    conn = ShellConnection()  # really runs the acquire script
    with transfer.ship_lock(conn, staging_dir=str(staging), run_id="1-1"):
        assert (tmp_path / "work" / "staging" / ("art" + transfer.SHIP_LOCK_SUFFIX)).is_dir()
    assert not (tmp_path / "work" / "staging" / ("art" + transfer.SHIP_LOCK_SUFFIX)).exists()


def test_ship_lock_is_exclusive_against_a_second_shipper(tmp_path: Path) -> None:
    """The property the whole thing exists for, against a real filesystem: while
    one run holds the lock, a second run's claim fails rather than proceeding into
    ship_source and resetting the staging dir under the first."""
    staging = tmp_path / "staging" / "art"
    conn = ShellConnection()
    with transfer.ship_lock(conn, staging_dir=str(staging), run_id="1-1"):
        assert not transfer._try_acquire_lock(
            conn, lock_dir=transfer._ship_lock_path(str(staging)), run_id="2-1", stale_minutes=30
        )
    # Released: the next run gets it.
    assert transfer._try_acquire_lock(
        conn, lock_dir=transfer._ship_lock_path(str(staging)), run_id="2-1", stale_minutes=30
    )


def test_ship_lock_waits_for_the_holder_then_acquires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer holds the lock for two polls; we wait rather than resetting under it."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    conn = LockConnection(busy_for=2)
    with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1", poll=7):
        pass
    assert conn.attempts == 3
    assert slept == [7, 7]


def test_ship_lock_breaks_a_lock_left_behind_by_a_dead_runner() -> None:
    """A runner that dies mid-ship cannot wedge the artifact forever, so the
    acquire also breaks a lock older than the staleness threshold — and then
    races for it again like everyone else rather than assuming it won."""
    conn = FakeConnection()
    with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1", stale_minutes=45):
        pass
    acquire = _lock_scripts(conn)[0]
    assert "-mmin +45" in acquire
    assert "rm -rf /remote/staging/art.shiplock " in acquire


def test_ship_lock_times_out_rather_than_shipping_anyway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never fall through to the ship on a lock we could not get: that is exactly
    the reset-under-a-peer this lock exists to prevent."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    conn = LockConnection(busy_for=10_000)
    with pytest.raises(CIError, match="waiting for the staging lock"):
        with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1", timeout=0, poll=1):
            raise AssertionError("body must not run without the lock")


def test_ship_lock_releases_when_the_ship_fails() -> None:
    """A failed ship must not leave the artifact locked until the staleness
    threshold, and the release must not replace the error that is propagating."""
    conn = FakeConnection()
    with pytest.raises(CIError, match="boom"):
        with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1"):
            raise CIError("boom")
    assert _lock_scripts(conn)[-1].startswith("rm -rf /remote/staging/art.shiplock ")


def test_ship_lock_dryrun_touches_nothing() -> None:
    conn = FakeConnection()
    with transfer.ship_lock(conn, staging_dir="/remote/staging/art", run_id="1-1", dryrun=True):
        pass
    assert conn.executed == []


# --------------------------------------------------------------------------- #
# fetch_install
# --------------------------------------------------------------------------- #
def test_fetch_install_collects_the_archive_the_job_wrote(tmp_path: Path) -> None:
    """No remote tar: the job owns producing CI_INSTALL_ARCHIVE, we only fetch it."""
    conn = TarballConnection()
    transfer.fetch_install(
        conn,
        remote_install_dir="/remote/install/art",
        local_install_dir=str(tmp_path / "local-install"),
        tar_dir=str(tmp_path / "stage"),
    )
    assert conn.executed == []
    assert conn.fetched == [("/remote/install/art.install.tgz", str(tmp_path / "stage" / "art.install.tgz"))]
    assert (tmp_path / "local-install").is_dir()


def test_fetch_install_archive_path_matches_the_jobscript_export(tmp_path: Path) -> None:
    """The name the job is told to write is the name we come looking for."""
    conn = TarballConnection()
    transfer.fetch_install(
        conn,
        remote_install_dir="/remote/install/art",
        local_install_dir=str(tmp_path / "local-install"),
        tar_dir=str(tmp_path / "stage"),
    )
    assert conn.fetched[0][0] == jobscript.install_archive_path("/remote/install/art")


# --------------------------------------------------------------------------- #
# fetch_tree / push_tree (the generic HPC<->runner primitives)
# --------------------------------------------------------------------------- #
def test_fetch_tree_tar_getfile_unpack_order(tmp_path: Path) -> None:
    conn = TarballConnection()
    transfer.fetch_tree(
        conn,
        remote_dir="/remote/ref/art",
        local_dir=str(tmp_path / "local"),
        tar_dir=str(tmp_path / "stage"),
    )
    # Remote tar first, then getfile back under the default .fetch.tgz name.
    assert conn.executed[0][:2] == ["bash", "-c"]
    assert "tar -czf" in conn.executed[0][2] and "/remote/ref/art" in conn.executed[0][2]
    assert conn.fetched == [("/remote/ref/art.fetch.tgz", str(tmp_path / "stage" / "art.fetch.tgz"))]
    assert (tmp_path / "local").is_dir()


def test_fetch_tree_dryrun_does_nothing(tmp_path: Path) -> None:
    conn = FakeConnection()
    transfer.fetch_tree(
        conn,
        remote_dir="/remote/ref/art",
        local_dir=str(tmp_path / "local"),
        tar_dir=str(tmp_path / "stage"),
        dryrun=True,
    )
    assert conn.executed == [] and conn.fetched == []
    assert not (tmp_path / "local").exists()


def test_push_tree_tar_sendfile_unpack_order(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "inputs", "in.txt", "payload")
    conn = FakeConnection()
    transfer.push_tree(
        conn,
        local_dir=str(src),
        remote_dir="/remote/inputs/art",
        tar_dir=str(tmp_path / "stage"),
    )
    # Remote dir is created, then the tarball is scp'd up under .push.tgz and untarred.
    assert ["mkdir", "-p", "/remote/inputs/art"] in conn.executed
    assert conn.sent == [(str(tmp_path / "stage" / "art.push.tgz"), "/remote/inputs/art.push.tgz")]
    untars = [c[2] for c in conn.executed if c[:2] == ["bash", "-c"]]
    assert any("tar -xzf" in u and "/remote/inputs/art" in u for u in untars)


def test_push_tree_dryrun_does_nothing(tmp_path: Path) -> None:
    src = _make_tree(tmp_path / "inputs", "in.txt", "payload")
    conn = FakeConnection()
    transfer.push_tree(
        conn,
        local_dir=str(src),
        remote_dir="/remote/inputs/art",
        tar_dir=str(tmp_path / "stage"),
        dryrun=True,
    )
    assert conn.executed == [] and conn.sent == []
    assert not (tmp_path / "stage").exists()  # no local tarball either


def test_remove_tree_rms_dir_and_transfer_tarballs(tmp_path: Path) -> None:
    conn = FakeConnection()
    transfer.remove_tree(conn, remote_dir="/remote/out/art")
    assert conn.executed == [["rm", "-rf", "/remote/out/art", "/remote/out/art.push.tgz", "/remote/out/art.fetch.tgz"]]


def test_remove_tree_dryrun_does_nothing() -> None:
    conn = FakeConnection()
    transfer.remove_tree(conn, remote_dir="/remote/out/art", dryrun=True)
    assert conn.executed == []


def test_push_then_fetch_roundtrip_preserves_tree(tmp_path: Path) -> None:
    """push_tree stages a tree on the 'cluster'; fetch_tree brings it back intact."""
    src = _make_tree(tmp_path / "inputs", "hello.txt", "content-xyz")

    conn = CopyingConnection()
    remote = tmp_path / "remote" / "inputs" / "art"
    transfer.push_tree(
        conn,
        local_dir=str(src),
        remote_dir=str(remote),
        tar_dir=str(tmp_path / "stage"),
    )
    assert (remote / "hello.txt").read_text() == "content-xyz"

    fetched = tmp_path / "back"
    transfer.fetch_tree(
        conn,
        remote_dir=str(remote),
        local_dir=str(fetched),
        tar_dir=str(tmp_path / "stage2"),
    )
    assert (fetched / "hello.txt").read_text() == "content-xyz"


def test_ship_then_fetch_roundtrip_preserves_tree(tmp_path: Path) -> None:
    """The tarball ship stages unpacks intact, and fetch_install brings a tree back."""
    src = _make_tree(tmp_path / "checkout", "hello.txt", "content-xyz")

    conn = CopyingConnection()
    staging = tmp_path / "remote" / "staging" / "art"
    transfer.ship_source(
        conn,
        local_source_dir=str(src),
        staging_dir=str(staging),
        run_id="42-1",
        tar_dir=str(tmp_path / "stage"),
    )
    # The staged tarball + marker are present; unpacking the tarball (what the
    # job does) reproduces the checkout.
    assert (staging / "source.tgz").is_file()
    assert (staging / "TRANSFER_COMPLETED").is_file()
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(staging / "source.tgz") as tar:
        tar.extractall(unpacked, filter="data")
    assert (unpacked / "hello.txt").read_text() == "content-xyz"

    # Stand in for the job's final step: archive the install tree at the agreed
    # path. Without it there is nothing to fetch -- that is the contract.
    archive = jobscript.install_archive_path(str(unpacked))
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(unpacked, arcname=".")

    fetched = tmp_path / "back"
    transfer.fetch_install(
        conn,
        remote_install_dir=str(unpacked),
        local_install_dir=str(fetched),
        tar_dir=str(tmp_path / "stage2"),
    )
    assert (fetched / "hello.txt").read_text() == "content-xyz"


def test_fetch_install_fails_when_the_job_wrote_no_archive(tmp_path: Path) -> None:
    """A job that skips the archive fails here rather than publishing nothing."""
    conn = CopyingConnection()
    with pytest.raises(FileNotFoundError):
        transfer.fetch_install(
            conn,
            remote_install_dir=str(tmp_path / "never-archived"),
            local_install_dir=str(tmp_path / "back"),
            tar_dir=str(tmp_path / "stage"),
        )


# --------------------------------------------------------------------------- #
# The packaged troika site config
#
# Nothing else in the suite touches it: every other HPC test monkeypatches
# load_site, so the site name it is handed is only a string. These load the real
# packaged config, so a site that is removed, renamed or mistyped fails here
# instead of in a consumer's HPC job with `troika.InvocationError: Unknown site`.
#
# Kept in step with ecmwf/build-package-hpc's config.yml, the de-facto register
# of which sites exist (ci-hpc-generic drives it).
# --------------------------------------------------------------------------- #
BATCH_SITES: Final = ["hpc-batch", "aa-batch", "ab-batch", "ac-batch", "ad-batch", "ag-batch", "lumi"]
DIRECT_SITES: Final = ["hpc-login", "lumi-login", "local-direct"]


@pytest.mark.parametrize("site_name", BATCH_SITES)
def test_packaged_config_provides_batch_site(site_name: str) -> None:
    site = load_site(site_name)
    # ensure_batch_site is what the orchestrator gates on: a `direct` site cannot
    # be driven by the build path (no job id to reattach by, no state to poll).
    ensure_batch_site(site, site_name)


@pytest.mark.parametrize("site_name", DIRECT_SITES)
def test_packaged_config_provides_direct_site(site_name: str) -> None:
    """The non-batch sites load too, and are correctly REJECTED for build use:
    the build path needs a job id to reattach by and a state to poll, which a
    direct site has neither of."""
    site = load_site(site_name)
    with pytest.raises(CIError):
        ensure_batch_site(site, site_name)


def test_packaged_config_rejects_an_unknown_site() -> None:
    """The failure mode this file exists to prevent, pinned as a real error."""
    with pytest.raises(Exception, match="Unknown site"):
        load_site("no-such-cluster")
