# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""build-on-hpc ``publish: archive``: a release's HPC build, fetched and checked, never via the artifact store."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from release_fixtures import HPC_LEG, RPM_LEG, write_release_repo

from ci_infrastructure import s3_store
from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript, orchestrate, release_tree
from ci_infrastructure.release import legs
from ci_infrastructure.release.detect import ReleaseEvent
from ci_infrastructure.release.version import parse_tag

SHA = "0123456789abcdef0123456789abcdef01234567"


class LocalConn:
    """troika's connection, with the cluster being this machine."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> Any:
        return subprocess.Popen([str(c) for c in command], stdout=stdout, stderr=stderr)

    def getfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        self.fetched.append(str(src))
        shutil.copyfile(src, dst)

    def sendfile(self, src: Any, dst: Any, dryrun: bool = False) -> None:
        shutil.copyfile(src, dst)


def hpc_leg(tmp_path: Path, tag: str = "2.5.0-rc1") -> dict[str, Any]:
    """The HPC release leg as the gate hands it over, installing under ``<tmp_path>/root``."""
    rm = legs.load(write_release_repo(tmp_path / "repo", leg=RPM_LEG + HPC_LEG))
    event = ReleaseEvent(version=parse_tag(tag), sha=SHA, head_sha=SHA)
    [leg] = legs.expand(rm, ["release-hpc"], event, hpc_root=str(tmp_path / "root"))["release-hpc"]
    return leg


def write_job_output(
    install_dir: Path, ctx: dict[str, Any], *, itest: bool = True, itest_over: dict[str, Any] | None = None
) -> str:
    """What the release job leaves beside CI_INSTALL_ARCHIVE; the archive's digest."""
    archive = Path(jobscript.install_archive_path(str(install_dir)))
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"pretend tar.zst " + ctx["job_key"].encode())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(digest + "\n")
    Path(f"{archive}.modules").write_text("gcc/8.5.0\n")
    Path(f"{archive}.build-meta.json").write_text(json.dumps({"release": {"job_key": ctx["job_key"]}}))
    if itest:
        receipt = {
            "result": "passed",
            "sha256": digest,
            "job_key": ctx["job_key"],
            "sha": ctx["sha"],
            **(itest_over or {}),
        }
        Path(f"{archive}.itest.json").write_text(json.dumps(receipt))
    return digest


def install(prefix: Path, ctx: dict[str, Any], sha256: str = "d" * 64, **over: Any) -> None:
    prefix.mkdir(parents=True)
    (prefix / release_tree.RELEASE_JSON).write_text(
        json.dumps({**release_tree.identity(ctx), "sha256": sha256, **over})
    )


# --- fetch and check ------------------------------------------------------------------------------------------


def test_fetch_archive_takes_the_archive_and_its_sidecars_unpacked_never(tmp_path: Path) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    digest = write_job_output(tmp_path / "remote" / "install" / "key", ctx)
    out = tmp_path / "out"
    build = release_tree.fetch_archive(
        LocalConn(), remote_install_dir=str(tmp_path / "remote" / "install" / "key"), local_dir=out, ctx=ctx
    )
    assert (build.state, build.sha256, build.archive) == ("build", digest, out / ctx["filename"])
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [ctx["filename"], "SHA256SUMS", "build-meta.json", "modules.txt", f"itest-{ctx['leg_id']}.json"]
    )
    assert (out / "SHA256SUMS").read_text() == f"{digest}  {ctx['filename']}\n"
    # Byte for byte what the job wrote.
    assert (out / ctx["filename"]).read_bytes().startswith(b"pretend tar.zst")


def test_an_archive_without_its_install_test_is_refused(tmp_path: Path) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    write_job_output(tmp_path / "remote" / "install" / "key", ctx, itest=False)
    with pytest.raises(CIError, match="no install test receipt"):
        release_tree.fetch_archive(
            LocalConn(),
            remote_install_dir=str(tmp_path / "remote" / "install" / "key"),
            local_dir=tmp_path / "o",
            ctx=ctx,
        )


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"sha256": "e" * 64}, "install test passed sha256"),
        ({"result": "failed"}, "did not pass"),
        ({"job_key": "other"}, "install test is of job other"),
        ({"sha": "f" * 40}, "install test is of job"),
    ],
)
def test_an_install_test_of_something_else_is_refused(tmp_path: Path, over: dict[str, str], match: str) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    write_job_output(tmp_path / "remote" / "install" / "key", ctx, itest_over=over)
    with pytest.raises(CIError, match=match):
        release_tree.fetch_archive(
            LocalConn(),
            remote_install_dir=str(tmp_path / "remote" / "install" / "key"),
            local_dir=tmp_path / "o",
            ctx=ctx,
        )


def test_an_archive_changed_after_the_job_is_refused(tmp_path: Path) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    write_job_output(tmp_path / "remote" / "install" / "key", ctx)
    out = tmp_path / "o"
    release_tree.fetch_archive(
        LocalConn(), remote_install_dir=str(tmp_path / "remote" / "install" / "key"), local_dir=out, ctx=ctx
    )
    (out / ctx["filename"]).write_bytes(b"other bytes")
    with pytest.raises(CIError, match="is not the .* the build wrote"):
        release_tree.check_local_build(out, ctx)


# --- the prefix on the cluster ---------------------------------------------------------------------------------


def test_probe_prefix(tmp_path: Path) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    prefix = tmp_path / "root" / "pkg" / "2.5.0-rc1" / "GNU" / "8.5"
    assert release_tree.probe_prefix(LocalConn(), str(prefix), ctx) == ("build", None)
    install(prefix, ctx)
    state, installed = release_tree.probe_prefix(LocalConn(), str(prefix), ctx)
    assert state == "already-published" and installed is not None and installed["sha256"] == "d" * 64


def test_probe_prefix_refuses_another_release_there(tmp_path: Path) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    prefix = tmp_path / "p"
    install(prefix, ctx, sha="f" * 40)
    with pytest.raises(CIError, match="holds another release .*tag a new rc"):
        release_tree.probe_prefix(LocalConn(), str(prefix), ctx)


def test_a_prefix_without_release_json_is_never_taken_over(tmp_path: Path) -> None:
    ctx = hpc_leg(tmp_path)["_release"]
    (tmp_path / "p" / "bin").mkdir(parents=True)
    with pytest.raises(CIError, match="no readable .release.json"):
        release_tree.probe_prefix(LocalConn(), str(tmp_path / "p"), ctx)


# --- submit-wait --fetch-archive ---------------------------------------------------------------------------------


class _Site:
    def __init__(self) -> None:
        self._connection = LocalConn()


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """submit-wait with this machine as the cluster; the "job" writes what the release job would."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
    leg = hpc_leg(tmp_path)
    work = tmp_path / "work"
    calls: dict[str, Any] = {"submitted": 0, "rendered": []}

    def no_store(name: str) -> bool:
        raise AssertionError("a release must not touch the artifact store")

    def submit(**kw: Any) -> tuple[int, str]:
        calls["submitted"] += 1
        write_job_output(work / "install" / leg["_release"]["job_key"], leg["_release"])
        Path(kw["output"]).parent.mkdir(parents=True, exist_ok=True)
        Path(kw["output"]).write_text("the job's log\n")
        return 7, "submitted"

    real_render = jobscript.render_job_script

    def render(**kw: Any) -> str:
        calls["rendered"].append(kw["repo_script"])
        return str(real_render(**kw))

    monkeypatch.setattr(s3_store, "object_exists", no_store)
    monkeypatch.setattr(orchestrate, "load_site", lambda *a, **k: _Site())
    monkeypatch.setattr(orchestrate, "ensure_batch_site", lambda *a: None)
    monkeypatch.setattr(orchestrate, "submit_or_reattach", submit)
    monkeypatch.setattr(orchestrate, "wait_for_job", lambda **kw: "SUCCESS")
    monkeypatch.setattr(orchestrate, "_stream_job_output", lambda *a: None)
    monkeypatch.setattr(orchestrate, "_stop_stream", lambda *a: None)
    monkeypatch.setattr(orchestrate, "_install_cancel_handler", lambda *a: None)
    monkeypatch.setattr(jobscript, "render_job_script", render)
    args = [
        "submit-wait",
        "--site",
        "hpc-batch",
        "--job-script",
        str(tmp_path / "repo" / ".ci" / "build.sh.j2"),
        "--artifact-name",
        leg["_release"]["job_key"],
        "--remote-work-dir",
        str(work),
        "--local-install-path",
        str(tmp_path / "local-install"),
        "--tar-dir",
        str(tmp_path / "tars"),
        "--matrix-leg",
        json.dumps(leg),
        "--fetch-archive",
        str(tmp_path / "release"),
    ]
    return {"args": args, "leg": leg, "calls": calls, "out": tmp_path / "gh-output", "dir": tmp_path / "release"}


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_submit_wait_fetches_the_release_archive(cli: dict[str, Any]) -> None:
    result = CliRunner().invoke(orchestrate.main, cli["args"])
    assert result.exit_code == 0, result.output
    ctx = cli["leg"]["_release"]
    out = _outputs(cli["out"])
    assert out["release-state"] == "build" and out["archive-dir"] == str(cli["dir"])
    assert out["archive-sha256"] == release_tree.sha256_file(cli["dir"] / ctx["filename"])
    assert (cli["dir"] / "build.log").read_text() == "the job's log\n"
    assert cli["calls"]["submitted"] == 1
    # The job script names the prefix as expanded on the cluster.
    [recipe] = cli["calls"]["rendered"]
    assert f"release_prefix={ctx['prefix']}" in recipe.splitlines()


def test_submit_wait_skips_a_release_installed_already(cli: dict[str, Any]) -> None:
    ctx = cli["leg"]["_release"]
    install(Path(ctx["prefix"]), ctx, sha256="a" * 64)
    result = CliRunner().invoke(orchestrate.main, cli["args"])
    assert result.exit_code == 0, result.output
    assert cli["calls"]["submitted"] == 0
    out = _outputs(cli["out"])
    assert (out["release-state"], out["archive-sha256"]) == ("already-published", "a" * 64)
    assert sorted(p.name for p in cli["dir"].iterdir()) == ["state.json"]
    assert release_tree.check_local_build(cli["dir"], ctx).state == "already-published"


def test_submit_wait_refuses_another_release_at_the_prefix(cli: dict[str, Any]) -> None:
    ctx = cli["leg"]["_release"]
    install(Path(ctx["prefix"]), ctx, job_key="pkg-2.5.0-rc1-hpc-hpc-atos-gnu-gxx-8-ffffffff")
    result = CliRunner().invoke(orchestrate.main, cli["args"])
    assert result.exit_code != 0
    assert "holds another release" in result.output
    assert cli["calls"]["submitted"] == 0


def test_a_rehearsal_never_looks_at_the_prefix(cli: dict[str, Any], tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path / "rehearsal")
    leg["_release"] = {**leg["_release"], "mode": "rehearsal"}
    install(Path(leg["_release"]["prefix"]), leg["_release"], sha="f" * 40)
    args = list(cli["args"])
    args[args.index("--matrix-leg") + 1] = json.dumps(leg)
    cli["leg"]["_release"] = leg["_release"]
    result = CliRunner().invoke(orchestrate.main, args)
    assert result.exit_code == 0, result.output
    assert cli["calls"]["submitted"] == 1


@pytest.mark.parametrize(
    ("extra", "match"),
    [(["--no-publish"], "exclude each other")],
)
def test_submit_wait_archive_refusals(cli: dict[str, Any], extra: list[str], match: str) -> None:
    result = CliRunner().invoke(orchestrate.main, [*cli["args"], *extra])
    assert result.exit_code != 0
    assert match in result.output


def test_archive_mode_needs_an_hpc_release_leg(cli: dict[str, Any]) -> None:
    leg = {k: v for k, v in cli["leg"].items() if k != "_release"}
    args = list(cli["args"])
    args[args.index("--matrix-leg") + 1] = json.dumps(leg)
    result = CliRunner().invoke(orchestrate.main, args)
    assert result.exit_code != 0
    assert "hpc-module release leg" in result.output


def test_archive_mode_refuses_a_batch_kind(cli: dict[str, Any]) -> None:
    leg = {**cli["leg"], "_resolved": {"packages": {"a": {"own-artifact-name": "a"}, "b": {"own-artifact-name": "b"}}}}
    args = list(cli["args"])
    args[args.index("--matrix-leg") + 1] = json.dumps(leg)
    result = CliRunner().invoke(orchestrate.main, args)
    assert result.exit_code != 0
    assert "publishes several is refused" in result.output
