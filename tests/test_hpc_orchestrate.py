# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the HPC (SLURM) orchestration backend.

These exercise submit/reattach, the wait state-machine and the job-script
rendering against a fake troika site and injected sentinel/state callables, so
they run with no cluster, no ssh and no scheduler.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Final

import pytest
from click.testing import CliRunner, Result

from ci_infrastructure import s3_store
from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript, transfer
from ci_infrastructure.hpc import orchestrate as orch
from ci_infrastructure.hpc.orchestrate import (
    GC_SUBDIRS,
    RemotePaths,
    Verdict,
    _echo_remote_output,
    _remote_sentinel_waiter,
    _stream_job_output,
    find_active_job_by_name,
    plan_remote_prefixes,
    run_gc,
    submit_or_reattach,
    wait_for_job,
)
from ci_infrastructure.hpc.site import resolve_remote_path


class FakeProc:
    """A finished process: what troika's connection.execute() hands back."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


class RecordingConnection:
    """Records the argv of every remote command, and reports success.

    ``squeue`` (the reattach lookup) returns ``squeue_stdout``; everything else
    returns an empty success. A default RecordingConnection returns no ``squeue``
    match, so submit_or_reattach submits fresh unless a test supplies output.
    """

    def __init__(self, *, squeue_stdout: bytes = b"", squeue_returncode: int = 0) -> None:
        self.executed: list[list[str]] = []
        self._squeue_stdout = squeue_stdout
        self._squeue_returncode = squeue_returncode

    def execute(self, command: Sequence[str], **_: object) -> FakeProc:
        argv = list(command)
        self.executed.append(argv)
        if argv and argv[0] == "squeue":
            return FakeProc(stdout=self._squeue_stdout, returncode=self._squeue_returncode)
        return FakeProc()


class FakeSlurmSite:
    """Minimal stand-in for troika's SlurmSite: scripts submit/_get_state/kill."""

    def __init__(
        self,
        *,
        next_jid: int = 111,
        states: dict[int, str | None] | None = None,
        connection: RecordingConnection | None = None,
    ) -> None:
        self.next_jid = next_jid
        self.states = states or {}
        self.submitted: list[str] = []
        self.killed: list[int] = []
        self.output_dirs: list[str] = []
        self._connection = connection if connection is not None else RecordingConnection()

    def create_output_dir(self, output: str, dryrun: bool = False) -> str:
        self.output_dirs.append(output)
        return output

    def submit(self, script: str, user: str | None, output: str, dryrun: bool = False) -> int:
        # troika scp's the script into the output dir, which must already exist.
        assert output in self.output_dirs, "create_output_dir must run before submit"
        self.submitted.append(script)
        if dryrun:
            return -1
        return self.next_jid

    def _get_state(self, jid: int, strict: bool = True, dryrun: bool = False) -> str | None:
        return self.states.get(jid)

    def kill(
        self,
        script: str,
        user: str | None,
        output: str | None = None,
        jid: int | None = None,
        dryrun: bool = False,
    ) -> tuple[int, str | None]:
        assert jid is not None
        self.killed.append(jid)
        return jid, "CANCELLED"


# --------------------------------------------------------------------------- #
# RemotePaths / resolve_remote_path
# --------------------------------------------------------------------------- #
def test_plan_remote_prefixes_maps_local_dirs_to_cluster_deps() -> None:
    """Runner-local prefixes become ordered <staging>/deps/<i> paths for the job."""
    remote, locals_, deps_dir = plan_remote_prefixes("/run/a:/run/b;/run/c", "/scratch/ci/staging/art")
    assert deps_dir == "/scratch/ci/staging/art/deps"
    assert locals_ == ["/run/a", "/run/b", "/run/c"]
    assert remote == "/scratch/ci/staging/art/deps/0:/scratch/ci/staging/art/deps/1:/scratch/ci/staging/art/deps/2"


def test_plan_remote_prefixes_empty_yields_empty_prefix() -> None:
    """A build with no deps leaves the job's CMAKE_PREFIX_PATH empty (unchanged)."""
    remote, locals_, deps_dir = plan_remote_prefixes("", "/scratch/ci/staging/art")
    assert remote == "" and locals_ == [] and deps_dir == "/scratch/ci/staging/art/deps"


def test_remote_paths_derive_layout_under_the_work_dir() -> None:
    paths = RemotePaths.derive("/ec/res4/scratch/me/ci", "art-abc-Release")
    assert paths.output == "/ec/res4/scratch/me/ci/hpc-jobs/art-abc-Release.out"
    assert paths.install == "/ec/res4/scratch/me/ci/install/art-abc-Release"
    assert paths.staging == "/ec/res4/scratch/me/ci/staging/art-abc-Release"


class _EchoConnection:
    """Runs the resolver's command locally, so $VARS really expand."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env or {}

    def execute(
        self, command: Sequence[str], stdout: int | None = None, stderr: int | None = None
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, stdout=stdout, stderr=stderr, env={**os.environ, **self.env})


def test_resolve_remote_path_expands_cluster_variables() -> None:
    """The spec must expand on the far side — that is the whole point of it."""
    conn = _EchoConnection({"SCRATCH": "/ec/res4/scratch/me"})
    assert resolve_remote_path(conn, "$SCRATCH/downstream-ci") == "/ec/res4/scratch/me/downstream-ci"


def test_resolve_remote_path_passes_a_literal_through() -> None:
    conn = _EchoConnection()
    assert resolve_remote_path(conn, "/ec/res4/scratch/me/ci") == "/ec/res4/scratch/me/ci"


def test_resolve_remote_path_rejects_an_unset_variable() -> None:
    """An unset var expands to nothing; scp'ing to '/downstream-ci' must not happen silently."""
    conn = _EchoConnection()  # NOSCRATCH is not in the environment
    with pytest.raises(CIError, match="not an absolute path"):
        resolve_remote_path(conn, "$NOSCRATCH")


def test_resolve_remote_path_rejects_a_relative_result() -> None:
    conn = _EchoConnection()
    with pytest.raises(CIError, match="not an absolute path"):
        resolve_remote_path(conn, "relative/ci")


@pytest.mark.parametrize("spec", ["$(touch /tmp/pwned)", "/tmp/x;rm -rf /", '/tmp/"; id; "', "/tmp/`id`"])
def test_resolve_remote_path_rejects_shell_metacharacters(spec: str) -> None:
    """The spec reaches a remote shell, so it must not be able to run commands."""
    with pytest.raises(CIError, match="not allowed"):
        resolve_remote_path(_EchoConnection(), spec)


# --------------------------------------------------------------------------- #
# find_active_job_by_name (the reattach lookup — scheduler as shared job store)
# --------------------------------------------------------------------------- #
def test_find_active_job_parses_jid() -> None:
    conn = RecordingConnection(squeue_stdout=b"777\n")
    assert find_active_job_by_name(conn, job_name="ci-art", user="deploy") == 777
    # Queries by name, scoped to the user, over the active states...
    squeue = next(c for c in conn.executed if c and c[0] == "squeue")
    assert "-n" in squeue and "ci-art" in squeue
    assert squeue[squeue.index("-u") + 1] == "deploy"
    # ...and asks for the jid ALONE. %k (Comment) is deliberately not requested:
    # it is scheduler-owned and sites rewrite it.
    assert squeue[squeue.index("-o") + 1] == "%i"


def test_find_active_job_ignores_a_site_decorated_comment() -> None:
    """ECMWF's sbatch wrapper appends its own accounting fields to the job
    Comment, so a run id read back from it arrives as
    ``32724386465-1;Gres=gres/ssdtmp:20G;`` — an embedded '/' that no file can be
    named for. Return the jid and nothing else, whatever the site appends.
    """
    conn = RecordingConnection(squeue_stdout=b"777|32724386465-1;Gres=gres/ssdtmp:20G;\n")
    assert find_active_job_by_name(conn, job_name="ci-art", user=None) == 777


def test_find_active_job_empty_output_is_none() -> None:
    assert find_active_job_by_name(RecordingConnection(), job_name="ci-art", user=None) is None


def test_find_active_job_squeue_error_is_none() -> None:
    conn = RecordingConnection(squeue_stdout=b"", squeue_returncode=1)
    assert find_active_job_by_name(conn, job_name="ci-art", user=None) is None


def test_find_active_job_multiple_takes_lowest_jid() -> None:
    # Two runs raced the submit (the residual window); every later run must
    # converge on the same job, so we take the lowest jid.
    conn = RecordingConnection(squeue_stdout=b"902\n811\n")
    assert find_active_job_by_name(conn, job_name="ci-art", user=None) == 811


# --------------------------------------------------------------------------- #
# submit_or_reattach
# --------------------------------------------------------------------------- #
def test_submit_when_no_active_job(tmp_path: Path) -> None:
    # squeue finds nothing (default RecordingConnection) -> fresh submit.
    site = FakeSlurmSite(next_jid=500)
    jid, action = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
    )
    assert (jid, action) == (500, "submitted")
    assert site.submitted  # a job was actually submitted


def test_output_file_is_created_before_submit(tmp_path: Path) -> None:
    """The tailed output must exist before the job can write to it.

    An unwritable output path has to fail here, not after a job has burned an
    allocation with nowhere to report its result.
    """
    site = FakeSlurmSite(next_jid=500)
    submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/jobs/art.out",
        job_name="ci-art",
        after_submit=lambda: site._connection.executed.append(["<ship>"]),
    )
    # The reattach lookup (squeue) runs first; then output dir + tail target.
    assert site._connection.executed[0][0] == "squeue"
    assert site._connection.executed[1:3] == [
        ["mkdir", "-p", "/scratch/jobs"],
        ["touch", "/scratch/jobs/art.out"],
    ]


def test_reattach_when_active_job_found_by_name(tmp_path: Path) -> None:
    # An active job named ci-art exists on the scheduler (any runner submitted it).
    site = FakeSlurmSite(next_jid=999, connection=RecordingConnection(squeue_stdout=b"777\n"))
    jid, action = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
    )
    assert (jid, action) == (777, "reattached")
    assert not site.submitted  # crucially, NO duplicate submission
    assert not site.output_dirs  # reattach touches nothing on the remote


def test_after_submit_runs_after_fresh_submit(tmp_path: Path) -> None:
    site = FakeSlurmSite(next_jid=500)
    order: list[str] = []

    def ship() -> None:
        # The job is submitted first, then the source is shipped: submit-then-poll.
        assert site.submitted, "source must be shipped AFTER the job is submitted"
        order.append("shipped")

    submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
        after_submit=ship,
    )
    assert order == ["shipped"]


def test_after_submit_skipped_on_reattach(tmp_path: Path) -> None:
    site = FakeSlurmSite(next_jid=999, connection=RecordingConnection(squeue_stdout=b"777\n"))
    calls: list[str] = []
    _, action = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
        after_submit=lambda: calls.append("shipped"),
    )
    assert action == "reattached"
    assert calls == []  # an in-flight job's sources must not be re-shipped


def test_dryrun_does_not_submit(tmp_path: Path) -> None:
    site = FakeSlurmSite()
    jid, action = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
        dryrun=True,
    )
    # A dry run goes through troika in pretend mode: no real job id, no reattach.
    assert (jid, action) == (-1, "dryrun")


# --------------------------------------------------------------------------- #
# wait_for_job state machine
# --------------------------------------------------------------------------- #
def test_wait_returns_success_immediately() -> None:
    verdict = wait_for_job(
        sentinel_waiter=lambda _s: "SUCCESS",
        state_getter=lambda: "RUNNING",
    )
    assert verdict == "SUCCESS"


def test_wait_returns_failure_from_sentinel() -> None:
    verdict = wait_for_job(
        sentinel_waiter=lambda _s: "FAILURE",
        state_getter=lambda: "RUNNING",
    )
    assert verdict == "FAILURE"


def test_wait_succeeds_after_one_timed_out_window() -> None:
    calls: list[float] = []

    def waiter(seconds: float) -> Verdict | None:
        calls.append(seconds)
        return None if len(calls) == 1 else "SUCCESS"  # first window times out, then success

    verdict = wait_for_job(
        sentinel_waiter=waiter,
        state_getter=lambda: "RUNNING",  # still alive after the first timeout
        guard_interval=1,
    )
    assert verdict == "SUCCESS"
    assert len(calls) == 2


def test_wait_declares_vanished_when_job_gone_without_sentinel() -> None:
    verdict = wait_for_job(
        sentinel_waiter=lambda _s: None,  # sentinel never appears
        state_getter=lambda: None,  # and the job is gone from the queue
        guard_interval=1,
    )
    assert verdict == "VANISHED"


def test_wait_catches_late_sentinel_after_job_leaves_queue() -> None:
    calls: list[float] = []

    def waiter(seconds: float) -> Verdict | None:
        calls.append(seconds)
        # First (guard) window: no sentinel. Grace window: a late SUCCESS flush.
        return "SUCCESS" if len(calls) >= 2 else None

    verdict = wait_for_job(
        sentinel_waiter=waiter,
        state_getter=lambda: None,  # job left the queue after the first window
        guard_interval=1,
    )
    assert verdict == "SUCCESS"


def test_wait_times_out() -> None:
    verdict = wait_for_job(
        sentinel_waiter=lambda _s: None,
        state_getter=lambda: "RUNNING",
        timeout=0,  # already past the deadline
    )
    assert verdict == "TIMEOUT"


# --------------------------------------------------------------------------- #
# _remote_sentinel_waiter: the real tail|sed pipeline, run locally
#
# The tests above inject a fake waiter, so they never exercise the pipeline that
# actually decides a job's fate. These run it for real against a local shell.
# --------------------------------------------------------------------------- #
class LocalShellSite:
    """A site whose connection runs the waiter's pipeline in a local shell.

    Mirrors troika's connection contract closely enough for
    ``_remote_sentinel_waiter``: ``execute`` takes an argv and returns a Popen.
    """

    def __init__(self) -> None:
        self._connection = self

    def execute(
        self, command: Sequence[str], stdout: int | None = None, stderr: int | None = None
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, stdout=stdout, stderr=stderr)


def test_sentinel_waiter_does_not_report_success_for_a_missing_output(tmp_path: Path) -> None:
    """A queued job has no output file yet — that must never read as SUCCESS.

    SLURM creates the output only once the job starts, so between submit and
    start the path does not exist. A pipeline that swallows tail's error lets
    sed see empty input and exit 0, which the waiter would map to SUCCESS —
    reporting a build finished before it ever ran.
    """
    wait = _remote_sentinel_waiter(LocalShellSite(), str(tmp_path / "not-created-yet.out"))
    assert wait(1.0) != "SUCCESS"


def test_sentinel_waiter_keeps_waiting_on_an_empty_output(tmp_path: Path) -> None:
    """The output exists (we pre-touch it) but holds no sentinel yet -> keep waiting."""
    output = tmp_path / "job.out"
    output.touch()
    wait = _remote_sentinel_waiter(LocalShellSite(), str(output))
    assert wait(1.0) is None


@pytest.mark.parametrize(
    ("sentinel", "expected"),
    [(jobscript.SENTINEL_SUCCESS, "SUCCESS"), (jobscript.SENTINEL_FAILURE, "FAILURE")],
)
def test_sentinel_waiter_reads_the_verdict_from_the_output(tmp_path: Path, sentinel: str, expected: str) -> None:
    output = tmp_path / "job.out"
    output.write_text(f"configuring...\nbuilding...\n{sentinel}\n")
    wait = _remote_sentinel_waiter(LocalShellSite(), str(output))
    assert wait(5.0) == expected


# --------------------------------------------------------------------------- #
# job-script rendering
# --------------------------------------------------------------------------- #
_REPO_BUILD: Final = """#!/bin/bash
#SBATCH --partition=compute
#SBATCH --time=00:30:00
module load prgenv/gnu cmake

cmake -B build -S . -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"
cmake --build build --target install
ctest --test-dir build
"""


def test_render_preserves_sbatch_and_injects_output() -> None:
    script = jobscript.render_job_script(
        repo_script=_REPO_BUILD,
        output_path="/scratch/ci/art.out",
        cmake_prefix_path="/scratch/install/dep",
        install_path="/scratch/install/art",
    )
    lines = script.splitlines()
    assert lines[0] == "#!/bin/bash"
    # repo's own #SBATCH directives are preserved above our injected ones
    assert "#SBATCH --partition=compute" in lines
    assert "#SBATCH --output=/scratch/ci/art.out" in script
    assert "#SBATCH --error=/scratch/ci/art.out" in script
    # our injected #SBATCH lines must come before the first executable line
    first_cmd = next(i for i, ln in enumerate(lines) if ln.startswith("cmake "))
    output_directive = next(i for i, ln in enumerate(lines) if ln.startswith("#SBATCH --output="))
    assert output_directive < first_cmd


def test_render_stamps_job_name_and_run_id_comment() -> None:
    """The scheduler-side reattach key: job name + the submitting run id in --comment."""
    script = jobscript.render_job_script(
        repo_script=_REPO_BUILD,
        output_path="/scratch/ci/art.out",
        cmake_prefix_path="/scratch/install/dep",
        install_path="/scratch/install/art",
        job_name="ci-pymath-abc-hpc-atos-gnu-py3.11-Release",
        staging_dir="/scratch/staging/art",
        run_id="99-1",
    )
    assert "#SBATCH --job-name=ci-pymath-abc-hpc-atos-gnu-py3.11-Release" in script
    assert "#SBATCH --comment=99-1" in script


def test_job_name_for_namespaces_the_artifact() -> None:
    assert jobscript.job_name_for("pymath-abc-Release") == "ci-pymath-abc-Release"


def test_render_injects_env_and_sentinels() -> None:
    script = jobscript.render_job_script(
        repo_script=_REPO_BUILD,
        output_path="/scratch/ci/art.out",
        cmake_prefix_path="/scratch/install/dep",
        install_path="/scratch/install/art",
        env={"OMP_NUM_THREADS": "8"},
    )
    assert 'export CMAKE_PREFIX_PATH="/scratch/install/dep' in script
    assert 'export CI_INSTALL_PREFIX="/scratch/install/art"' in script
    assert 'export OMP_NUM_THREADS="8"' in script
    assert "trap _ci_on_err ERR" in script
    # success sentinel is the very last executable line
    assert script.rstrip().endswith(f'echo "{jobscript.SENTINEL_SUCCESS}"')
    assert jobscript.SENTINEL_FAILURE in script


def test_render_waits_for_marker_and_unpacks_when_staging_given() -> None:
    script = jobscript.render_job_script(
        repo_script=_REPO_BUILD,
        output_path="/scratch/ci/art.out",
        cmake_prefix_path="/scratch/install/dep",
        install_path="/scratch/install/art",
        staging_dir="/scratch/staging/art",
        run_id="99-1",
        marker_wait_timeout=1200,
    )
    lines = script.splitlines()
    # Blocks on the staging-dir-scoped marker, with the timeout the caller passed.
    # NOT run-scoped: any runner that finds this job must be able to satisfy it
    # without knowing which run submitted it (see find_active_job_by_name).
    assert '_ci_marker="/scratch/staging/art/TRANSFER_COMPLETED"' in script
    assert "TRANSFER_COMPLETED_99-1" not in script
    assert "$(date +%s) + 1200" in script
    assert f'echo "{jobscript.SENTINEL_FAILURE}"' in script  # marker-never-arrives branch
    # Unpacks the shipped tarball into node-local $TMPDIR and cds there.
    assert 'export CI_SOURCE_DIR="${TMPDIR:-/tmp}/ci-src-99-1"' in script
    assert 'tar -xzf "/scratch/staging/art/source.tgz" -C "$CI_SOURCE_DIR"' in script
    # The wait/unpack happens before the build body.
    cd_i = next(i for i, ln in enumerate(lines) if ln == 'cd "$CI_SOURCE_DIR"')
    first_cmd = next(i for i, ln in enumerate(lines) if ln.startswith("cmake "))
    assert cd_i < first_cmd
    # The failure trap is armed before the marker wait, so an unpack error signals.
    trap_i = next(i for i, ln in enumerate(lines) if ln == "trap _ci_on_err ERR")
    marker_i = next(i for i, ln in enumerate(lines) if ln.startswith("_ci_marker="))
    assert trap_i < marker_i


def test_render_omits_marker_wait_without_staging() -> None:
    script = jobscript.render_job_script(
        repo_script=_REPO_BUILD,
        output_path="/o",
        cmake_prefix_path="/p",
        install_path="/i",
    )
    assert "CI_SOURCE_DIR" not in script
    assert "TRANSFER_COMPLETED" not in script


def test_render_without_shebang_still_starts_with_one() -> None:
    script = jobscript.render_job_script(
        repo_script="#SBATCH --time=00:10:00\necho hi\n",
        output_path="/o",
        cmake_prefix_path="/p",
        install_path="/i",
    )
    assert script.startswith("#!/bin/bash\n")
    assert "#SBATCH --time=00:10:00" in script


# --------------------------------------------------------------------------- #
# gc (nightly cleanup)
# --------------------------------------------------------------------------- #
class _GcConnection:
    def __init__(self, returncode: int = 0) -> None:
        self.commands: list[str] = []
        self._returncode = returncode

    def execute(self, command: Sequence[str], **_: object) -> FakeProc:
        self.commands.append(command[2])  # the bash -c payload
        return FakeProc(returncode=self._returncode)


def test_run_gc_sweeps_every_tree_a_build_creates() -> None:
    """GC must cover everything RemotePaths lays down, plus only declared extras.

    The build side is derived from RemotePaths rather than hard-coded, so a new
    remote tree cannot be added without the sweep noticing it. Extras are allowed
    but must be named here, so nothing joins the sweep silently either.
    """
    conn = _GcConnection()
    run_gc(conn, remote_work_dir="/scratch/ci", older_than_days=7)
    joined = "\n".join(conn.commands)

    paths = RemotePaths.derive("/scratch/ci", "art")
    build_dirs = {str(PurePosixPath(p).parent) for p in paths}
    swept = {f"/scratch/ci/{sub}" for sub in GC_SUBDIRS}

    assert build_dirs <= swept, f"unswept build trees: {sorted(build_dirs - swept)}"
    assert swept - build_dirs == {"/scratch/ci/transfer-e2e"}, (
        "a subdir swept but not written by a build needs a reason here; transfer-e2e holds "
        "smoke-test-hpc.yml's per-run trees, which are left behind on failure on purpose"
    )
    for base in swept:
        assert base in joined
    assert "-mtime +7" in joined
    assert "-exec rm -rf {} +" in joined
    assert "-print" not in joined


def test_run_gc_dryrun_lists_without_deleting() -> None:
    conn = _GcConnection()
    run_gc(conn, remote_work_dir="/scratch/ci", older_than_days=0, dryrun=True)
    joined = "\n".join(conn.commands)
    assert "-print" in joined
    assert "rm -rf" not in joined


# --------------------------------------------------------------------------- #
# Echoing the job's own output into the CI log
# --------------------------------------------------------------------------- #
class _CatConnection:
    """A connection whose `cat` returns a canned job log."""

    def __init__(self, out: bytes) -> None:
        self._out = out
        self.executed: list[list[str]] = []

    def execute(self, command: Sequence[str], **_: object) -> FakeProc:
        self.executed.append(list(command))
        return FakeProc(self._out)


def test_echo_remote_output_prints_the_captured_job_log(capsys: pytest.CaptureFixture[str]) -> None:
    """The compiler/ctest output the waiter greps past must still reach the CI log."""
    conn = _CatConnection(b"compiling foo.cpp\nctest: 3/3 passed\nFinished: SUCCESS\n")
    _echo_remote_output(conn, "/scratch/ci/hpc-jobs/pkg.out")
    printed = capsys.readouterr().out
    assert "compiling foo.cpp" in printed
    assert "ctest: 3/3 passed" in printed
    assert conn.executed == [["cat", "/scratch/ci/hpc-jobs/pkg.out"]]


def test_echo_remote_output_swallows_read_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """A failure to read the log must not raise and mask the job's verdict."""

    class _Boom:
        def execute(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("connection dropped")

    _echo_remote_output(_Boom(), "/scratch/ci/hpc-jobs/pkg.out")
    assert "could not read job output" in capsys.readouterr().out


def test_stream_job_output_tails_live_and_quits_at_a_sentinel() -> None:
    """The streamer tails the output and stops the remote pipeline at the sentinel."""
    conn = RecordingConnection()
    _stream_job_output(conn, "/scratch/ci/hpc-jobs/pkg.out")
    argv = conn.executed[-1]
    assert argv[:2] == ["bash", "-c"]
    pipeline = argv[2]
    assert "tail -F -n +1" in pipeline
    assert "/scratch/ci/hpc-jobs/pkg.out" in pipeline
    # Prints every line but quits the pipeline once either sentinel is seen.
    assert f"/^{jobscript.SENTINEL_SUCCESS}$/q" in pipeline
    assert f"/^{jobscript.SENTINEL_FAILURE}$/q" in pipeline


# --------------------------------------------------------------------------- #
# submit-wait: publish vs. --no-publish (test-only) mode
#
# submit-wait is driven with its real collaborators stubbed (no cluster, no S3,
# no ssh), so we can assert the two things --no-publish changes: it never
# consults the artifact cache, and it never fetches an install tree on success.
# --------------------------------------------------------------------------- #
def _invoke_submit_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    no_publish: bool,
    squeue_stdout: bytes = b"",
    marker_present: bool = False,
    marker_answers: list[bool] | None = None,
    run_id: str = "1-1",
    connection: RecordingConnection | None = None,
) -> tuple[Result, dict[str, int], list[str]]:
    """Run submit-wait to a SUCCESS verdict with every collaborator stubbed.

    Returns the CliRunner result and a dict counting the calls that distinguish
    publish from test-only mode (`object_exists`, `fetch_install`) and the
    reattach paths (`ship_source`), plus the run ids `ship_source` was handed.

    `marker_present` answers every `marker_exists` probe the same way.
    `marker_answers` overrides it with one answer per probe (the last is reused
    once exhausted), which is how a peer that lands its marker *between* two
    probes — the fast-path check and the re-check under the staging lock — is
    expressed.
    """
    calls = {"object_exists": 0, "fetch_install": 0, "ship_source": 0}
    shipped_run_ids: list[str] = []

    recipe = tmp_path / "build.sh"
    recipe.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\nctest --test-dir build\n")
    (tmp_path / "src").mkdir()

    site = FakeSlurmSite(next_jid=500, connection=connection or RecordingConnection(squeue_stdout=squeue_stdout))

    def _ship_source(*_a: object, **k: object) -> None:
        calls["ship_source"] += 1
        shipped_run_ids.append(str(k.get("run_id", "")))

    def _object_exists(_name: str) -> bool:
        calls["object_exists"] += 1
        return False  # never a cache hit in publish mode -> proceeds to submit

    def _fetch_install(*_a: object, **_k: object) -> None:
        calls["fetch_install"] += 1

    # Patch the canonical modules (orchestrate looks up object_exists / ship_source
    # etc. on these same module objects at call time), plus the orchestrate-level
    # names it calls directly. Patching s3_store / transfer via the imported module
    # (not orch.s3_store) also keeps mypy's no-implicit-reexport happy.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(s3_store, "object_exists", _object_exists)
    monkeypatch.setattr(orch, "load_site", lambda *a, **k: site)
    monkeypatch.setattr(orch, "ensure_batch_site", lambda *a, **k: None)
    monkeypatch.setattr(orch, "resolve_remote_path", lambda _conn, spec: spec)
    monkeypatch.setattr(transfer, "ship_source", _ship_source)
    answers = list(marker_answers) if marker_answers else [marker_present]

    def _marker_exists(*_a: object, **_k: object) -> bool:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(transfer, "marker_exists", _marker_exists)
    monkeypatch.setattr(transfer, "touch_remote_file", lambda *a, **k: None)
    monkeypatch.setattr(transfer, "fetch_install", _fetch_install)
    monkeypatch.setattr(orch, "_install_cancel_handler", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_stream_job_output", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_stop_stream", lambda *a, **k: None)
    monkeypatch.setattr(orch, "wait_for_job", lambda **k: "SUCCESS")

    args = [
        "--site",
        "hpc-batch",
        "--job-script",
        str(recipe),
        "--artifact-name",
        "pymath-abc-hpc-atos-gnu-py3.11",
        "--remote-work-dir",
        "/scratch/ci",
        "--local-install-path",
        str(tmp_path / "install"),
        "--source-dir",
        str(tmp_path / "src"),
        "--run-id",
        run_id,
        "--tar-dir",
        str(tmp_path / "tars"),
        "--cmake-prefix-path",
        "",
    ]
    if no_publish:
        args.append("--no-publish")
    result = CliRunner().invoke(orch.submit_wait, args)
    return result, calls, shipped_run_ids


def test_submit_wait_test_only_skips_cache_and_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--no-publish runs the job for its verdict but consults no cache and fetches nothing."""
    result, calls, _ = _invoke_submit_wait(monkeypatch, tmp_path, no_publish=True)
    assert result.exit_code == 0, result.output
    assert (calls["object_exists"], calls["fetch_install"]) == (0, 0)
    assert "nothing to publish" in result.output


def test_submit_wait_publish_mode_checks_cache_and_fetches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default (publish) mode still cache-checks and fetches the built tree — the contrast."""
    result, calls, _ = _invoke_submit_wait(monkeypatch, tmp_path, no_publish=False)
    assert result.exit_code == 0, result.output
    assert (calls["object_exists"], calls["fetch_install"]) == (1, 1)


def test_submit_wait_reattach_does_not_reship_when_a_marker_is_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """squeue reports an in-flight job for this artifact, so we reattach. Its
    source is already staged (a marker is present), so nothing may be re-shipped
    — the job may be mid-build, and `ship_source` renames the staging tree aside.

    The marker is named for the staging dir, never for the submitting run id: the
    job's SLURM Comment is decorated by ECMWF's sbatch wrapper, so a run id read
    back from it would name a marker that never existed and every reattach would
    re-ship.
    """
    result, calls, _ = _invoke_submit_wait(
        monkeypatch, tmp_path, no_publish=False, squeue_stdout=b"777\n", marker_present=True
    )
    assert result.exit_code == 0, result.output
    assert "reattached job 777" in result.output
    assert calls["ship_source"] == 0
    assert "re-shipping source" not in result.output


def test_submit_wait_reattach_reships_under_our_own_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Submit succeeded but the submitter died before shipping: the waiting job
    still needs its source. We re-ship under OUR --run-id — the only run id in
    play now — and the job finds it because the marker is named for the staging
    dir rather than for whoever submitted."""
    result, calls, shipped_run_ids = _invoke_submit_wait(
        monkeypatch, tmp_path, no_publish=False, squeue_stdout=b"777\n", marker_present=False
    )
    assert result.exit_code == 0, result.output
    assert "re-shipping source" in result.output
    assert calls["ship_source"] == 1
    assert shipped_run_ids == ["1-1"]


def test_submit_wait_fresh_submit_does_not_reship_over_a_complete_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Losing the submit race must not destroy the winner's staged inputs.

    squeue shows nothing (so this run submits its own job), but a completed
    transfer is already in the artifact's staging dir because another run got
    there first. ship_source RESETS that dir before writing — it renames the tree
    aside — so shipping here would delete deps/<i> from under a job already
    reading them. That is what failed an ecflow hpc-atos-nvidia job with
    "Could not find a package configuration file provided by ecbuild" while the
    other job, on the same staging dir, was compiling happily.

    Safe to skip: staging is per-artifact and the artifact name embeds the source
    SHA and deps hash, so what is already there is the same content.
    """
    result, calls, _ = _invoke_submit_wait(
        monkeypatch, tmp_path, no_publish=False, squeue_stdout=b"", marker_present=True
    )
    assert result.exit_code == 0, result.output
    assert calls["ship_source"] == 0
    assert "already complete" in result.output


def test_submit_wait_fresh_submit_ships_when_staging_is_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The contrast: no marker means no (or a partial) transfer, which is exactly
    the case the reset exists for — so this must still ship."""
    result, calls, shipped = _invoke_submit_wait(
        monkeypatch, tmp_path, no_publish=False, squeue_stdout=b"", marker_present=False
    )
    assert result.exit_code == 0, result.output
    assert calls["ship_source"] == 1
    assert shipped == ["1-1"]


def test_submit_wait_ships_under_the_staging_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The ship is serialised per staging dir, not merely marker-guarded.

    Two runs that start shipping the same artifact together both see no marker,
    so the marker check alone lets both into ship_source — whose reset then pulls
    the peer's already-staged tarballs out from under it, surfacing as
    `deps/<i>.tgz: Cannot open: No such file or directory` from the peer's remote
    untar. Holding the lock across the whole ship is what prevents that, so a ship
    has to claim the lock and give it back around ship_source.
    """
    conn = RecordingConnection()
    result, calls, _ = _invoke_submit_wait(
        monkeypatch, tmp_path, no_publish=False, marker_present=False, connection=conn
    )
    assert result.exit_code == 0, result.output
    assert calls["ship_source"] == 1
    lock = shlex.quote("/scratch/ci/staging/pymath-abc-hpc-atos-gnu-py3.11" + transfer.SHIP_LOCK_SUFFIX)
    scripts = [argv[2] for argv in conn.executed if argv[:2] == ["bash", "-c"]]
    acquired = next(i for i, script in enumerate(scripts) if f"\nif mkdir {lock} " in script)
    released = next(i for i, script in enumerate(scripts) if script.startswith(f"rm -rf {lock} "))
    assert acquired < released


def test_submit_wait_skips_shipping_when_a_peer_finishes_while_we_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The re-check under the lock is what makes the lock worth taking.

    The fast-path probe finds no marker (the peer is still shipping), so this run
    queues for the staging lock. By the time it gets in, the peer has finished and
    dropped the marker — the same completed transfer, by construction, since the
    artifact name embeds the source SHA and the deps hash. Re-shipping now would
    reset the staging dir under a job that is already reading `deps/<i>`, so the
    second probe has to be believed and the ship skipped.
    """
    result, calls, _ = _invoke_submit_wait(monkeypatch, tmp_path, no_publish=False, marker_answers=[False, True])
    assert result.exit_code == 0, result.output
    assert calls["ship_source"] == 0
    assert "while we waited for the staging lock" in result.output


@pytest.mark.parametrize("bad", ["a/b", "run;Gres=gres/ssdtmp:20G;", "a b", "x$(id)"])
def test_submit_wait_rejects_an_unsafe_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: str) -> None:
    """The run id names a tarball and remote directories, so a separator or shell
    metacharacter in it must be refused by name rather than surfacing as a tar
    error from somewhere deep in the transfer."""
    result, calls, _ = _invoke_submit_wait(monkeypatch, tmp_path, no_publish=False, run_id=bad)
    # CIError is a click.ClickException, so click renders it and exits non-zero.
    assert result.exit_code != 0
    assert "not a safe path segment" in result.output
    assert calls["ship_source"] == 0  # rejected before anything was written


# --------------------------------------------------------------------------- #
# fetch-tree / push-tree / remove-tree (the standalone transfer subcommands)
#
# The commands themselves are thin: click parses the flags, the site and the path
# resolver are stubbed, and the transfer primitive is tested directly in
# test_hpc_transfer.py. What is worth pinning is the part nothing else covers —
# which $GITHUB_OUTPUT key each command publishes for later steps to read, and
# that a dry run publishes none.
# --------------------------------------------------------------------------- #
class _FakeSite:
    def __init__(self) -> None:
        self._connection = RecordingConnection()


def _stub_site_and_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "load_site", lambda *a, **k: _FakeSite())
    monkeypatch.setattr(orch, "resolve_remote_path", lambda _conn, spec: spec)  # identity


@pytest.mark.parametrize("dryrun", [False, True])
@pytest.mark.parametrize(
    ("command", "primitive", "argv", "expected_output"),
    [
        (
            "fetch_tree_cmd",
            "fetch_tree",
            ["--remote-dir", "/scratch/ref", "--local-dir", "/tmp/back", "--tar-dir", "/tmp/tars"],
            "local-dir=/tmp/back\n",
        ),
        (
            "push_tree_cmd",
            "push_tree",
            ["--local-dir", "/tmp/inputs", "--remote-dir", "/scratch/inputs", "--tar-dir", "/tmp/tars"],
            # push publishes the RESOLVED cluster dir: that is what a later step
            # (a build, or remove-hpc-tree) has to be pointed at.
            "remote-dir=/scratch/inputs\n",
        ),
        ("remove_tree_cmd", "remove_tree", ["--remote-dir", "/scratch/ci/out"], ""),
    ],
)
def test_transfer_command_output_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    primitive: str,
    argv: list[str],
    expected_output: str,
    dryrun: bool,
) -> None:
    _stub_site_and_resolver(monkeypatch)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(transfer, primitive, lambda _conn, **kw: calls.append(kw))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    result = CliRunner().invoke(
        getattr(orch, command), ["--site", "hpc-batch", *argv, *(["--dryrun"] if dryrun else [])]
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["dryrun"] is dryrun
    if dryrun:
        # A dry run must publish nothing: a later step keyed off these outputs
        # would otherwise act on a transfer that never happened.
        assert not out_file.exists()
    else:
        assert (out_file.read_text() if out_file.exists() else "") == expected_output


def test_remove_tree_refuses_top_level_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must stop a bare '$SCRATCH'/'/tmp' from wiping a whole tree."""
    _stub_site_and_resolver(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(transfer, "remove_tree", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    result = CliRunner().invoke(orch.remove_tree_cmd, ["--site", "hpc-batch", "--remote-dir", "/tmp"])
    # CIError is a click.ClickException, so click exits non-zero at the boundary.
    assert result.exit_code != 0
    assert called["n"] == 0  # crucially, nothing was removed


# --------------------------------------------------------------------------- #
# The click group's own surface
# --------------------------------------------------------------------------- #


def test_every_cli_command_dispatches_through_the_group() -> None:
    """`python -m ci_infrastructure.hpc <cmd>` must resolve for all six commands.

    Every other CLI test here invokes a command *function* directly, which never
    touches the group — so a command renamed or not registered would be invisible
    to this suite, and only surface as "No such command" in a consumer's job. The
    composite actions call these names verbatim (see
    actions/{push,fetch,remove}-hpc-tree and build-on-hpc), so the set is a
    contract, not an implementation detail.
    """
    expected = {"submit-wait", "cancel", "gc", "fetch-tree", "push-tree", "remove-tree"}
    assert set(orch.main.commands) == expected

    # Registration is not enough: each name must actually dispatch. `--help`
    # exercises the same lookup a runner does, with no site or cluster.
    runner = CliRunner()
    for name in sorted(expected):
        result = runner.invoke(orch.main, [name, "--help"])
        assert result.exit_code == 0, f"{name}: {result.output}"
        assert "No such command" not in result.output


# --------------------------------------------------------------------------- #
# Top-level remote paths (an unset work-dir variable)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("remote_dir", ["/transfer-e2e-32144742771", "/scratch", "/", "//"])
def test_push_tree_refuses_a_top_level_remote_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote_dir: str
) -> None:
    """An unset vars.HPC_CI_REMOTE_WORK_DIR renders '/transfer-e2e-<run id>'.

    Left alone, the cluster answers with `mkdir: cannot create directory
    '/transfer-e2e-...': Read-only file system`, which says nothing about the
    actual cause. Fail on the runner instead, naming the variable.
    """
    _stub_site_and_resolver(monkeypatch)
    called: list[object] = []
    monkeypatch.setattr(transfer, "push_tree", lambda *a, **k: called.append(k))

    result = CliRunner().invoke(
        orch.push_tree_cmd,
        ["--site", "hpc-batch", "--local-dir", str(tmp_path), "--remote-dir", remote_dir, "--tar-dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "HPC_CI_REMOTE_WORK_DIR" in str(result.output) + str(result.exception)
    assert called == [], "nothing may be transferred once the path is refused"


def test_push_tree_accepts_a_nested_remote_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One level below root is the boundary; the normal case must still pass."""
    _stub_site_and_resolver(monkeypatch)
    called: list[object] = []
    monkeypatch.setattr(transfer, "push_tree", lambda *a, **k: called.append(k))

    result = CliRunner().invoke(
        orch.push_tree_cmd,
        [
            "--site",
            "hpc-batch",
            "--local-dir",
            str(tmp_path),
            "--remote-dir",
            "/scratch/transfer-e2e-1",
            "--tar-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(called) == 1
