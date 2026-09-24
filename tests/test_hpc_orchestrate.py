# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""HPC orchestration: reattach, the wait state machine, the sentinel pipeline and job-script rendering."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest
from click.testing import CliRunner
from conftest import FakeProc

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript
from ci_infrastructure.hpc import orchestrate as orch
from ci_infrastructure.hpc.orchestrate import (
    GC_SUBDIRS,
    RemotePaths,
    Verdict,
    _echo_remote_output,
    _remote_sentinel_waiter,
    _require_nested_remote_path,
    find_active_job_by_name,
    plan_remote_prefixes,
    submit_or_reattach,
    wait_for_job,
)
from ci_infrastructure.hpc.site import resolve_remote_path


class SqueueConnection:
    """`squeue` answers with `stdout`/`returncode`; every other command succeeds."""

    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self._squeue = FakeProc(stdout=stdout, returncode=returncode)

    def execute(self, command: Sequence[str], **_: object) -> FakeProc:
        return self._squeue if command and command[0] == "squeue" else FakeProc()


class LocalShell:
    """A troika site and connection in one, running commands locally."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._connection = self
        self.env = env or {}

    def execute(
        self, command: Sequence[str], stdout: int | None = None, stderr: int | None = None
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(command, stdout=stdout, stderr=stderr, env={**os.environ, **self.env})


@pytest.mark.parametrize(
    ("spec", "remote", "locals_"),
    [
        ("", "", []),
        ("/run/a:/run/b;/run/c", "/s/art/deps/0:/s/art/deps/1:/s/art/deps/2", ["/run/a", "/run/b", "/run/c"]),
    ],
)
def test_plan_remote_prefixes_maps_local_dirs_to_cluster_deps(spec: str, remote: str, locals_: list[str]) -> None:
    assert plan_remote_prefixes(spec, "/s/art") == (remote, locals_, "/s/art/deps")


def test_remote_paths_derive_layout_under_the_work_dir() -> None:
    paths = RemotePaths.derive("/ec/res4/scratch/me/ci", "art-abc-Release")
    assert paths.output == "/ec/res4/scratch/me/ci/hpc-jobs/art-abc-Release.out"
    assert paths.install == "/ec/res4/scratch/me/ci/install/art-abc-Release"
    assert paths.staging == "/ec/res4/scratch/me/ci/staging/art-abc-Release"


def test_resolve_remote_path_expands_cluster_variables() -> None:
    conn = LocalShell({"SCRATCH": "/ec/res4/scratch/me"})
    assert resolve_remote_path(conn, "$SCRATCH/downstream-ci") == "/ec/res4/scratch/me/downstream-ci"


def test_resolve_remote_path_passes_a_literal_through() -> None:
    assert resolve_remote_path(LocalShell(), "/ec/res4/scratch/me/ci") == "/ec/res4/scratch/me/ci"


@pytest.mark.parametrize("spec", ["$NOSCRATCH", "relative/ci"])
def test_resolve_remote_path_rejects_a_non_absolute_result(spec: str) -> None:
    with pytest.raises(CIError, match="not an absolute path"):
        resolve_remote_path(LocalShell(), spec)


@pytest.mark.parametrize("spec", ["$(touch /tmp/pwned)", "/tmp/x;rm -rf /", '/tmp/"; id; "', "/tmp/`id`"])
def test_resolve_remote_path_rejects_shell_metacharacters(spec: str) -> None:
    with pytest.raises(CIError, match="not allowed"):
        resolve_remote_path(LocalShell(), spec)


@pytest.mark.parametrize("remote_dir", ["/transfer-e2e-32144742771", "/scratch", "/", "//", "/tmp"])
def test_a_top_level_remote_dir_is_refused(remote_dir: str) -> None:
    with pytest.raises(CIError, match="HPC_CI_REMOTE_WORK_DIR"):
        _require_nested_remote_path("push-tree", remote_dir, remote_dir)


def test_a_nested_remote_dir_is_accepted() -> None:
    _require_nested_remote_path("push-tree", "/scratch/transfer-e2e-1", "/scratch/transfer-e2e-1")


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [(b"777\n", 0, 777), (b"", 0, None), (b"", 1, None), (b"902\n811\n", 0, 811)],
    ids=["one", "none", "squeue-error", "lowest-of-racers"],
)
def test_find_active_job_by_name(stdout: bytes, returncode: int, expected: int | None) -> None:
    conn = SqueueConnection(stdout, returncode)
    assert find_active_job_by_name(conn, job_name="ci-art", user=None) == expected


class _NoSubmitSite:
    def __init__(self) -> None:
        self._connection = SqueueConnection(b"777\n")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"reattach must not call site.{name}")


def test_reattach_submits_and_ships_nothing(tmp_path: Path) -> None:
    shipped: list[str] = []
    jid, action = submit_or_reattach(
        site=_NoSubmitSite(),
        script_path=tmp_path / "job.sh",
        user=None,
        output="/scratch/out",
        job_name="ci-art",
        after_submit=lambda: shipped.append("x"),
    )
    assert (jid, action) == (777, "reattached")
    assert shipped == []


@pytest.mark.parametrize("verdict", ["SUCCESS", "FAILURE"])
def test_wait_returns_the_sentinel_verdict(verdict: Verdict) -> None:
    assert wait_for_job(sentinel_waiter=lambda _s: verdict, state_getter=lambda: "RUNNING") == verdict


@pytest.mark.parametrize("state", ["RUNNING", None], ids=["still-queued", "late-sentinel"])
def test_wait_succeeds_after_one_timed_out_window(state: str | None) -> None:
    calls: list[float] = []

    def waiter(seconds: float) -> Verdict | None:
        calls.append(seconds)
        return None if len(calls) == 1 else "SUCCESS"

    assert wait_for_job(sentinel_waiter=waiter, state_getter=lambda: state, guard_interval=1) == "SUCCESS"
    assert len(calls) == 2


def test_wait_declares_vanished_when_job_gone_without_sentinel() -> None:
    assert wait_for_job(sentinel_waiter=lambda _s: None, state_getter=lambda: None, guard_interval=1) == "VANISHED"


def test_wait_times_out() -> None:
    assert wait_for_job(sentinel_waiter=lambda _s: None, state_getter=lambda: "RUNNING", timeout=0) == "TIMEOUT"


def test_sentinel_waiter_does_not_report_success_for_a_missing_output(tmp_path: Path) -> None:
    wait = _remote_sentinel_waiter(LocalShell(), str(tmp_path / "not-created-yet.out"), 4242)
    assert wait(1.0) != "SUCCESS"


def test_sentinel_waiter_keeps_waiting_on_an_empty_output(tmp_path: Path) -> None:
    output = tmp_path / "job.out"
    output.touch()
    assert _remote_sentinel_waiter(LocalShell(), str(output), 4242)(1.0) is None


@pytest.mark.parametrize(
    ("sentinel", "expected"),
    [(jobscript.SENTINEL_SUCCESS, "SUCCESS"), (jobscript.SENTINEL_FAILURE, "FAILURE")],
)
def test_sentinel_waiter_reads_the_verdict_from_the_output(tmp_path: Path, sentinel: str, expected: str) -> None:
    output = tmp_path / "job.out"
    output.write_text(f"configuring...\nbuilding...\n{sentinel} 4242\n")
    assert _remote_sentinel_waiter(LocalShell(), str(output), 4242)(5.0) == expected


def test_sentinel_waiter_ignores_a_sentinel_from_another_job(tmp_path: Path) -> None:
    output = tmp_path / "job.out"
    output.write_text(f"{jobscript.SENTINEL_FAILURE} 1111\n")
    assert _remote_sentinel_waiter(LocalShell(), str(output), 2222)(1.0) is None


_REPO_BUILD: Final = """#!/bin/bash
#SBATCH --partition=compute
#SBATCH --time=00:30:00
module load prgenv/gnu cmake

cmake -B build -S . -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"
cmake --build build --target install
ctest --test-dir build
"""


def _script(**kw: Any) -> str:
    args: dict[str, Any] = {
        "repo_script": _REPO_BUILD,
        "output_path": "/scratch/ci/art.out",
        "cmake_prefix_path": "/scratch/install/dep",
        "install_path": "/scratch/install/art",
        **kw,
    }
    return jobscript.render_job_script(**args)


def test_render_preserves_sbatch_and_injects_output() -> None:
    script = _script()
    lines = script.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert "#SBATCH --partition=compute" in lines
    assert "#SBATCH --output=/scratch/ci/art.out" in script
    assert "#SBATCH --error=/scratch/ci/art.out" in script
    first_cmd = next(i for i, ln in enumerate(lines) if ln.startswith("cmake "))
    output_directive = next(i for i, ln in enumerate(lines) if ln.startswith("#SBATCH --output="))
    assert output_directive < first_cmd


def test_render_stamps_job_name_and_run_id_comment() -> None:
    script = _script(
        job_name="ci-pymath-abc-hpc-atos-gnu-py3.11-Release", staging_dir="/scratch/staging/art", run_id="99-1"
    )
    assert "#SBATCH --job-name=ci-pymath-abc-hpc-atos-gnu-py3.11-Release" in script
    assert "#SBATCH --comment=99-1" in script


def test_job_name_for_namespaces_the_artifact() -> None:
    assert jobscript.job_name_for("pymath-abc-Release") == "ci-pymath-abc-Release"


def test_render_injects_env_and_sentinels() -> None:
    script = _script(env={"OMP_NUM_THREADS": "8"})
    assert 'export CMAKE_PREFIX_PATH="/scratch/install/dep' in script
    assert 'export CI_INSTALL_PREFIX="/scratch/install/art"' in script
    assert 'export OMP_NUM_THREADS="8"' in script
    assert "trap _ci_on_err ERR" in script
    assert script.rstrip().endswith(f'echo "{jobscript.SENTINEL_SUCCESS} {jobscript.SENTINEL_JOB_ID}"')
    assert jobscript.SENTINEL_FAILURE in script


def test_render_waits_for_marker_and_unpacks_when_staging_given() -> None:
    script = _script(staging_dir="/scratch/staging/art", run_id="99-1", marker_wait_timeout=1200)
    lines = script.splitlines()
    assert '_ci_marker="/scratch/staging/art/TRANSFER_COMPLETED"' in script
    assert "TRANSFER_COMPLETED_99-1" not in script
    assert "$(date +%s) + 1200" in script
    assert f'echo "{jobscript.SENTINEL_FAILURE} {jobscript.SENTINEL_JOB_ID}"' in script
    assert 'export CI_SOURCE_DIR="${TMPDIR:-/tmp}/ci-src-99-1"' in script
    assert 'tar -xzf "/scratch/staging/art/source.tgz" -C "$CI_SOURCE_DIR"' in script
    cd_i = next(i for i, ln in enumerate(lines) if ln == 'cd "$CI_SOURCE_DIR"')
    first_cmd = next(i for i, ln in enumerate(lines) if ln.startswith("cmake "))
    assert cd_i < first_cmd
    trap_i = next(i for i, ln in enumerate(lines) if ln == "trap _ci_on_err ERR")
    marker_i = next(i for i, ln in enumerate(lines) if ln.startswith("_ci_marker="))
    assert trap_i < marker_i


def test_render_omits_marker_wait_without_staging() -> None:
    script = _script()
    assert "CI_SOURCE_DIR" not in script
    assert "TRANSFER_COMPLETED" not in script


def test_render_without_shebang_still_starts_with_one() -> None:
    script = _script(repo_script="#SBATCH --time=00:10:00\necho hi\n")
    assert script.startswith("#!/bin/bash\n")
    assert "#SBATCH --time=00:10:00" in script


def test_jobscript_exports_the_install_archive_path() -> None:
    assert 'export CI_INSTALL_ARCHIVE="/scratch/install/art.install.tar.zst"' in _script()
    assert jobscript.install_archive_path("/scratch/install/art") == "/scratch/install/art.install.tar.zst"


def test_gc_sweeps_every_tree_a_build_creates() -> None:
    build_dirs = {str(PurePosixPath(p).parent) for p in RemotePaths.derive("/scratch/ci", "art")}
    swept = {f"/scratch/ci/{sub}" for sub in GC_SUBDIRS}
    assert build_dirs <= swept, f"unswept build trees: {sorted(build_dirs - swept)}"
    # transfer-e2e holds smoke-test-hpc.yml's per-run trees, left behind on failure on purpose.
    assert swept - build_dirs == {"/scratch/ci/transfer-e2e"}


def test_echo_remote_output_swallows_read_errors(capsys: pytest.CaptureFixture[str]) -> None:
    class _Boom:
        def execute(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("connection dropped")

    _echo_remote_output(_Boom(), "/scratch/ci/hpc-jobs/pkg.out")
    assert "could not read job output" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["a/b", "run;Gres=gres/ssdtmp:20G;", "a b", "x$(id)"])
def test_submit_wait_rejects_an_unsafe_run_id(tmp_path: Path, bad: str) -> None:
    result = CliRunner().invoke(
        orch.submit_wait,
        [
            *("--site", "hpc-batch", "--job-script", str(tmp_path / "build.sh"), "--artifact-name", "art"),
            *("--remote-work-dir", "/scratch/ci", "--local-install-path", str(tmp_path / "install")),
            *("--tar-dir", str(tmp_path / "tars"), "--run-id", bad),
        ],
    )
    assert result.exit_code != 0
    assert "not a safe path segment" in result.output


def test_every_cli_command_dispatches_through_the_group() -> None:
    """The composite actions call these names verbatim."""
    expected = {"submit-wait", "cancel", "gc", "fetch-tree", "push-tree", "remove-tree", "render"}
    assert set(orch.main.commands) == expected
    runner = CliRunner()
    for name in sorted(expected):
        result = runner.invoke(orch.main, [name, "--help"])
        assert result.exit_code == 0, f"{name}: {result.output}"
