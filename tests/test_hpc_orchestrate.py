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
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

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


class RecordingProc:
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

    def execute(self, command: Sequence[str], **_: object) -> RecordingProc:
        argv = list(command)
        self.executed.append(argv)
        if argv and argv[0] == "squeue":
            return RecordingProc(stdout=self._squeue_stdout, returncode=self._squeue_returncode)
        return RecordingProc()


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
def test_find_active_job_parses_jid_and_run_id() -> None:
    conn = RecordingConnection(squeue_stdout=b"777|9001-2\n")
    assert find_active_job_by_name(conn, job_name="ci-art", user="deploy") == (777, "9001-2")
    # Queries by name, scoped to the user, over the active states.
    squeue = next(c for c in conn.executed if c and c[0] == "squeue")
    assert "-n" in squeue and "ci-art" in squeue
    assert squeue[squeue.index("-u") + 1] == "deploy"


def test_find_active_job_empty_output_is_none() -> None:
    assert find_active_job_by_name(RecordingConnection(), job_name="ci-art", user=None) is None


def test_find_active_job_squeue_error_is_none() -> None:
    conn = RecordingConnection(squeue_stdout=b"", squeue_returncode=1)
    assert find_active_job_by_name(conn, job_name="ci-art", user=None) is None


def test_find_active_job_multiple_takes_lowest_jid() -> None:
    # Two runs raced the submit (the residual window); every later run must
    # converge on the same job, so we take the lowest jid.
    conn = RecordingConnection(squeue_stdout=b"902|b-1\n811|a-1\n")
    assert find_active_job_by_name(conn, job_name="ci-art", user=None) == (811, "a-1")


# --------------------------------------------------------------------------- #
# submit_or_reattach
# --------------------------------------------------------------------------- #
def test_submit_when_no_active_job(tmp_path: Path) -> None:
    # squeue finds nothing (default RecordingConnection) -> fresh submit.
    site = FakeSlurmSite(next_jid=500)
    jid, action, run_id = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
    )
    assert (jid, action, run_id) == (500, "submitted", None)
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
    site = FakeSlurmSite(next_jid=999, connection=RecordingConnection(squeue_stdout=b"777|orig-run\n"))
    jid, action, run_id = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
    )
    assert (jid, action, run_id) == (777, "reattached", "orig-run")
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
    site = FakeSlurmSite(next_jid=999, connection=RecordingConnection(squeue_stdout=b"777|orig-run\n"))
    calls: list[str] = []
    _, action, _ = submit_or_reattach(
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
    jid, action, run_id = submit_or_reattach(
        site=site,
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
        dryrun=True,
    )
    # A dry run goes through troika in pretend mode: no real job id, no reattach.
    assert (jid, action, run_id) == (-1, "dryrun", None)


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
_REPO_BUILD = """#!/bin/bash
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
        job_name="ci-pymath-abc-atos-hpc-gnu-py3.11-Release",
        staging_dir="/scratch/staging/art",
        run_id="99-1",
    )
    assert "#SBATCH --job-name=ci-pymath-abc-atos-hpc-gnu-py3.11-Release" in script
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
    # Blocks on the run-scoped marker, with the timeout the caller passed.
    assert '_ci_marker="/scratch/staging/art/TRANSFER_COMPLETED_99-1"' in script
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
class _GcProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


class _GcConnection:
    def __init__(self, returncode: int = 0) -> None:
        self.commands: list[str] = []
        self._returncode = returncode

    def execute(self, command: Sequence[str], **_: object) -> _GcProc:
        self.commands.append(command[2])  # the bash -c payload
        return _GcProc(returncode=self._returncode)


def test_run_gc_sweeps_every_tree_a_build_creates() -> None:
    """GC must cover exactly what RemotePaths lays down — no more, no less.

    Derived from RemotePaths rather than hard-coded, so a new remote tree cannot
    be added without the sweep noticing it.
    """
    conn = _GcConnection()
    run_gc(conn, remote_work_dir="/scratch/ci", older_than_days=7)
    joined = "\n".join(conn.commands)

    paths = RemotePaths.derive("/scratch/ci", "art")
    swept = {f"/scratch/ci/{sub}" for sub in GC_SUBDIRS}
    assert swept == {str(PurePosixPath(p).parent) for p in paths}
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
class _CatProc:
    returncode = 0

    def __init__(self, out: bytes) -> None:
        self._out = out

    def communicate(self) -> tuple[bytes, bytes]:
        return self._out, b""


class _CatConnection:
    """A connection whose `cat` returns a canned job log."""

    def __init__(self, out: bytes) -> None:
        self._out = out
        self.executed: list[list[str]] = []

    def execute(self, command: Sequence[str], **_: object) -> _CatProc:
        self.executed.append(list(command))
        return _CatProc(self._out)


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, no_publish: bool
) -> tuple[Result, dict[str, int]]:
    """Run submit-wait to a SUCCESS verdict with every collaborator stubbed.

    Returns the CliRunner result and a dict counting the calls that distinguish
    publish from test-only mode (`object_exists`, `fetch_install`).
    """
    calls = {"object_exists": 0, "fetch_install": 0}

    recipe = tmp_path / "build.sh"
    recipe.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\nctest --test-dir build\n")
    (tmp_path / "src").mkdir()

    site = FakeSlurmSite(next_jid=500)

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
    monkeypatch.setattr(transfer, "ship_source", lambda *a, **k: None)
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
        "pymath-abc-atos-hpc-gnu-py3.11",
        "--remote-work-dir",
        "/scratch/ci",
        "--local-install-path",
        str(tmp_path / "install"),
        "--source-dir",
        str(tmp_path / "src"),
        "--run-id",
        "1-1",
        "--tar-dir",
        str(tmp_path / "tars"),
        "--cmake-prefix-path",
        "",
    ]
    if no_publish:
        args.append("--no-publish")
    result = CliRunner().invoke(orch.submit_wait, args)
    return result, calls


def test_submit_wait_test_only_skips_cache_and_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--no-publish runs the job for its verdict but consults no cache and fetches nothing."""
    result, calls = _invoke_submit_wait(monkeypatch, tmp_path, no_publish=True)
    assert result.exit_code == 0, result.output
    assert calls == {"object_exists": 0, "fetch_install": 0}
    assert "nothing to publish" in result.output


def test_submit_wait_publish_mode_checks_cache_and_fetches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default (publish) mode still cache-checks and fetches the built tree — the contrast."""
    result, calls = _invoke_submit_wait(monkeypatch, tmp_path, no_publish=False)
    assert result.exit_code == 0, result.output
    assert calls == {"object_exists": 1, "fetch_install": 1}


# --------------------------------------------------------------------------- #
# fetch-tree / push-tree (the standalone transfer subcommands)
#
# Driven through the CLI with load_site / resolve_remote_path / the transfer
# primitive stubbed, so we assert the two things the command wires: the kwargs
# forwarded to the primitive, and the $GITHUB_OUTPUT it writes.
# --------------------------------------------------------------------------- #
class _FakeSite:
    def __init__(self) -> None:
        self._connection = RecordingConnection()


def _stub_site_and_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "load_site", lambda *a, **k: _FakeSite())
    monkeypatch.setattr(orch, "resolve_remote_path", lambda _conn, spec: spec)  # identity


def test_fetch_tree_forwards_kwargs_and_writes_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_site_and_resolver(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transfer, "fetch_tree", lambda _conn, **kw: captured.update(kw))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    result = CliRunner().invoke(
        orch.fetch_tree_cmd,
        [
            "--site",
            "local-direct",
            "--remote-dir",
            "/scratch/ref",
            "--local-dir",
            str(tmp_path / "back"),
            "--tar-dir",
            str(tmp_path / "tars"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "remote_dir": "/scratch/ref",
        "local_dir": str(tmp_path / "back"),
        "tar_dir": str(tmp_path / "tars"),
        "dryrun": False,
    }
    assert out_file.read_text() == f"local-dir={tmp_path / 'back'}\n"


def test_fetch_tree_dryrun_forwards_flag_and_writes_no_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_site_and_resolver(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transfer, "fetch_tree", lambda _conn, **kw: captured.update(kw))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    result = CliRunner().invoke(
        orch.fetch_tree_cmd,
        [
            "--site",
            "local-direct",
            "--remote-dir",
            "/scratch/ref",
            "--local-dir",
            str(tmp_path / "back"),
            "--tar-dir",
            str(tmp_path / "tars"),
            "--dryrun",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["dryrun"] is True
    assert not out_file.exists()  # nothing written on a dry run


def test_push_tree_forwards_kwargs_and_writes_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_site_and_resolver(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transfer, "push_tree", lambda _conn, **kw: captured.update(kw))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    result = CliRunner().invoke(
        orch.push_tree_cmd,
        [
            "--site",
            "local-direct",
            "--local-dir",
            str(tmp_path / "inputs"),
            "--remote-dir",
            "/scratch/inputs",
            "--tar-dir",
            str(tmp_path / "tars"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "local_dir": str(tmp_path / "inputs"),
        "remote_dir": "/scratch/inputs",
        "tar_dir": str(tmp_path / "tars"),
        "dryrun": False,
    }
    # push writes the RESOLVED cluster dir (its useful output for later steps).
    assert out_file.read_text() == "remote-dir=/scratch/inputs\n"


def test_push_tree_dryrun_forwards_flag_and_writes_no_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_site_and_resolver(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transfer, "push_tree", lambda _conn, **kw: captured.update(kw))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    result = CliRunner().invoke(
        orch.push_tree_cmd,
        [
            "--site",
            "local-direct",
            "--local-dir",
            str(tmp_path / "inputs"),
            "--remote-dir",
            "/scratch/inputs",
            "--tar-dir",
            str(tmp_path / "tars"),
            "--dryrun",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["dryrun"] is True
    assert not out_file.exists()


def test_remove_tree_forwards_resolved_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_site_and_resolver(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transfer, "remove_tree", lambda _conn, **kw: captured.update(kw))

    result = CliRunner().invoke(
        orch.remove_tree_cmd,
        ["--site", "hpc-batch", "--remote-dir", "/scratch/ci/out"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"remote_dir": "/scratch/ci/out", "dryrun": False}


def test_remove_tree_dryrun_forwards_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub_site_and_resolver(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(transfer, "remove_tree", lambda _conn, **kw: captured.update(kw))

    result = CliRunner().invoke(
        orch.remove_tree_cmd,
        ["--site", "hpc-batch", "--remote-dir", "/scratch/ci/out", "--dryrun"],
    )
    assert result.exit_code == 0, result.output
    assert captured["dryrun"] is True


def test_remove_tree_refuses_top_level_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must stop a bare '$SCRATCH'/'/tmp' from wiping a whole tree."""
    _stub_site_and_resolver(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(transfer, "remove_tree", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    result = CliRunner().invoke(orch.remove_tree_cmd, ["--site", "hpc-batch", "--remote-dir", "/tmp"])
    # CIError is a click.ClickException, so click exits non-zero at the boundary.
    assert result.exit_code != 0
    assert called["n"] == 0  # crucially, nothing was removed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# --------------------------------------------------------------------------- #
# The click group's own surface
# --------------------------------------------------------------------------- #


def test_cli_group_registers_every_command() -> None:
    """`python -m ci_infrastructure.hpc <cmd>` must resolve for all six commands.

    Every other CLI test in this file invokes a command *function* directly
    (`CliRunner().invoke(orch.push_tree_cmd, …)`), which never touches the group.
    A command renamed or not registered on the group is therefore invisible to
    this suite -- and that is exactly how a stale image reporting
    "No such command 'push-tree'" got as far as it did. The composite actions call
    these names verbatim (see actions/{push,fetch,remove}-hpc-tree and
    build-on-hpc), so this set is a contract, not an implementation detail.
    """
    assert set(orch.main.commands) == {
        "submit-wait",
        "cancel",
        "gc",
        "fetch-tree",
        "push-tree",
        "remove-tree",
    }


def test_cli_group_commands_are_invocable_through_the_group() -> None:
    """Registration alone is not enough: each name must dispatch from the group.

    `--help` through the group exercises the same lookup a runner does, without
    needing a site, a cluster or any troika config.
    """
    runner = CliRunner()
    for name in sorted(orch.main.commands):
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
