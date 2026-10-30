# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The release gate, against a mocked GitHub API and Nexus probe."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from release_fixtures import HPC_LEG, RPM_LEG, SAMPLE_WORKFLOW, write_release_repo

from ci_infrastructure.release import ReleaseError, gate, nexus

REPO = "o/pkg"
SHA = "a" * 40
HEAD = "b" * 40
ECBUILD_SHA = "e" * 40
TAG_OBJ = "f" * 40
WORKFLOW_REF = f"{REPO}/.github/workflows/cd-v3.yml@refs/tags/2.5.0-rc1"


def _run(**over: Any) -> dict[str, Any]:
    run = {
        "id": 100,
        "event": "push",
        "head_sha": SHA,
        "head_branch": "develop",
        "status": "completed",
        "conclusion": "success",
        "head_repository": {"full_name": REPO},
        "html_url": "https://github.com/o/pkg/actions/runs/100",
        "pull_requests": [],
    }
    run.update(over)
    return run


class Api:
    """Answers by path prefix; records every call."""

    def __init__(self, ci_runs: list[list[dict[str, Any]]] | None = None) -> None:
        # One list of runs per poll; the last repeats.
        self.ci_runs = ci_runs if ci_runs is not None else [[_run()]]
        self.cd_runs: list[dict[str, Any]] = []
        self.tags: dict[str, Any] = {
            "3.16.0": {"object": {"type": "tag", "sha": TAG_OBJ}},
        }
        self.tag_objects: dict[str, Any] = {TAG_OBJ: {"object": {"type": "commit", "sha": ECBUILD_SHA}}}
        self.calls: list[str] = []
        self.polls = 0

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if path.startswith(f"repos/{REPO}/actions/workflows/ci.yml/runs?head_sha="):
            sha = path.split("head_sha=")[1].split("&")[0]
            runs = self.ci_runs[min(self.polls, len(self.ci_runs) - 1)]
            return {"workflow_runs": [r for r in runs if r["head_sha"] == sha]}
        if path.startswith(f"repos/{REPO}/actions/workflows/cd-v3.yml/runs?branch="):
            # GitHub's branch filter is an exact, case-sensitive match.
            branch = path.split("branch=")[1].split("&")[0]
            return {"workflow_runs": [r for r in self.cd_runs if r["head_branch"] == branch]}
        if path.startswith("repos/o/ecbuild/git/ref/tags/"):
            return self.tags.get(path.rsplit("/", 1)[1])
        if path.startswith("repos/o/ecbuild/git/tags/"):
            return self.tag_objects.get(path.rsplit("/", 1)[1])
        if path.startswith("repos/o/ecbuild/commits/"):
            sha = path.rsplit("/", 1)[1]
            return {"sha": sha} if sha == ECBUILD_SHA else None
        raise AssertionError(f"unexpected API call {path}")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now


def _inputs(root: Path, **over: Any) -> gate.GateInputs:
    inp = gate.GateInputs(
        repo_root=root,
        manifest=root / ".ci" / "manifest.toml",
        kinds=["release-rpm"],
        repository=REPO,
        event_name="push",
        ref="refs/tags/2.5.0-rc1",
        sha=SHA,
        payload={},
        workflow_ref=WORKFLOW_REF,
        run_id="555",
        nexus_read_urls={"rocky-9": "https://nexus-test.ecmwf.int/repository/rpm"},
        ci_wait_seconds=600,
        poll_seconds=60,
        allow_refs=("feature/cd-v3",),
    )
    return replace(inp, **over)


def _gate(
    root: Path,
    api: Api | None = None,
    probe: Callable[[str, str], str] | None = None,
    clock: Clock | None = None,
    **over: Any,
) -> gate.GateResult:
    clock = clock or Clock()
    api = api or Api()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        api.polls += 1

    return gate.run_gate(
        _inputs(root, **over), api=api, probe=probe or (lambda t, f: "absent"), sleep=sleep, clock=clock
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_release_repo(tmp_path)
    return tmp_path


def test_a_prerelease_passes_every_check(root: Path) -> None:
    result = _gate(root)
    out = result.outputs()
    assert (out["mode"], out["publish"], out["sha"], out["tag"], out["version"]) == (
        "prerelease",
        "true",
        SHA,
        "2.5.0-rc1",
        "2.5.0",
    )
    [leg] = json.loads(out["json"])["release-rpm"]["include"]
    assert leg["_release"]["state"] == "build" and "_resolved" not in leg
    # The annotated tag, dereferenced.
    assert json.loads(out["source-deps"]) == [
        {"repo": "o/ecbuild", "package": "ecbuild", "ref": "3.16.0", "sha": ECBUILD_SHA}
    ]
    prov = json.loads(out["provenance"])
    assert prov["ci_run"] == {"id": 100, "url": "https://github.com/o/pkg/actions/runs/100", "event": "push"}
    assert prov["run_url"] == "https://github.com/o/pkg/actions/runs/555"
    assert prov["legs"] == {"release-rpm": [{"leg_id": "rpm-rocky-9-gxx-11", "state": "build"}]}


def test_an_uppercase_rc_tag_is_accepted(root: Path) -> None:
    result = _gate(root, ref="refs/tags/2.5.0-RC3")
    assert result.legs["release-rpm"][0]["_release"]["pkg_version"] == "2.5.0~rc3"


def test_a_published_leg_is_marked_never_dropped(root: Path) -> None:
    result = _gate(root, probe=lambda target, filename: "present")
    [leg] = result.legs["release-rpm"]
    assert leg["_release"]["state"] == "already-published"


def test_an_unknown_probe_builds(root: Path) -> None:
    assert _gate(root, probe=lambda t, f: "unknown").legs["release-rpm"][0]["_release"]["state"] == "build"


def test_hpc_legs_install_under_the_hpc_root_and_are_not_probed(tmp_path: Path) -> None:
    """The gate cannot reach the cluster: the HPC build job looks at the prefix itself."""
    write_release_repo(tmp_path, leg=RPM_LEG + HPC_LEG)
    probed: list[str] = []

    def probe(target: str, filename: str) -> str:
        probed.append(filename)
        return "absent"

    result = _gate(tmp_path, probe=probe, kinds=["release-rpm", "release-hpc"], hpc_root="$SCRATCH/cd-v3-x")
    [leg] = result.legs["release-hpc"]
    assert leg["_release"]["prefix"] == "$SCRATCH/cd-v3-x/pkg/2.5.0-rc1/GNU/8.5"
    assert leg["_release"]["state"] == "build"
    assert probed == ["pkg-2.5.0~rc1-1.x86_64.rpm"]
    prov = json.loads(result.outputs()["provenance"])
    assert prov["hpc_root"] == "$SCRATCH/cd-v3-x"
    assert "None" not in prov["containers"]
    assert "eccr.ecmwf.int/private-ci-images/ubuntu24.04-internal-tools:latest" in prov["containers"]


def test_an_hpc_root_in_production_is_refused(tmp_path: Path) -> None:
    write_release_repo(tmp_path, leg=RPM_LEG + HPC_LEG)
    with pytest.raises(ReleaseError, match="allow-production"):
        _gate(tmp_path, kinds=["release-hpc"], hpc_root="/usr/local/apps")


# --- refusals ------------------------------------------------------------------------------------------------


def test_kill_switch(root: Path) -> None:
    with pytest.raises(ReleaseError, match="kill switch is on: incident 42"):
        _gate(root, kill_switch="on", kill_switch_reason="incident 42")


def test_release_mode_needs_allow_production(root: Path) -> None:
    with pytest.raises(ReleaseError, match="prereleases \\(-rcN\\) only"):
        _gate(root, ref="refs/tags/2.5.0")


def test_tag_must_match_version(root: Path) -> None:
    with pytest.raises(ReleaseError, match="bump VERSION"):
        _gate(root, ref="refs/tags/2.5.1-rc1")


def test_a_non_release_tag_is_refused(root: Path) -> None:
    with pytest.raises(ReleaseError, match="not a release tag"):
        _gate(root, ref="refs/tags/2.5.0-beta1")


def test_failed_ci_is_refused_at_once(root: Path) -> None:
    clock = Clock()
    with pytest.raises(ReleaseError, match="CI did not pass .*failure"):
        _gate(root, api=Api([[_run(conclusion="failure")]]), clock=clock)
    assert clock.sleeps == []


def test_ci_still_running_is_waited_for(root: Path) -> None:
    api = Api([[_run(status="in_progress", conclusion=None)], [_run(status="in_progress", conclusion=None)], [_run()]])
    clock = Clock()
    assert _gate(root, api=api, clock=clock).event.sha == SHA
    assert clock.sleeps == [60, 60]


def test_ci_not_started_times_out(root: Path) -> None:
    clock = Clock()
    with pytest.raises(ReleaseError, match="no passed ci.yml on .* within 600s \\(none\\)"):
        _gate(root, api=Api([[]]), clock=clock)
    assert len(clock.sleeps) == 10


def test_a_pr_ci_run_does_not_count_for_a_tag(root: Path) -> None:
    pr_run = _run(event="pull_request", pull_requests=[{"number": 7}])
    with pytest.raises(ReleaseError, match="within"):
        _gate(root, api=Api([[pr_run]]), ci_wait_seconds=0)


def test_a_fork_ci_run_does_not_count(root: Path) -> None:
    fork_run = _run(head_repository={"full_name": "fork/pkg"})
    with pytest.raises(ReleaseError, match="within"):
        _gate(root, api=Api([[fork_run]]), ci_wait_seconds=0)


def test_a_moved_tag_is_refused(root: Path) -> None:
    api = Api()
    api.cd_runs = [_run(head_branch="2.5.0-rc1", head_sha="c" * 40)]
    with pytest.raises(ReleaseError, match="tags are immutable"):
        _gate(root, api=api)


def test_a_case_variant_tag_on_another_commit_is_refused(root: Path) -> None:
    # 2.5.0-RC1 and 2.5.0-rc1 are the same rpm: an earlier 2.5.0-RC1 run on another commit moved it.
    api = Api()
    api.cd_runs = [_run(head_branch="2.5.0-RC1", head_sha="c" * 40)]
    with pytest.raises(ReleaseError, match=r"2\.5\.0-RC1@c{40}.*tags are immutable"):
        _gate(root, api=api)
    assert any("branch=2.5.0-RC1&" in c for c in api.calls)


def test_an_unlistable_tag_history_is_refused(root: Path) -> None:
    class FailingApi(Api):
        def __call__(self, path: str) -> Any:
            return None if "cd-v3.yml/runs" in path else super().__call__(path)

    with pytest.raises(ReleaseError, match="refusing to release blind"):
        _gate(root, api=FailingApi())


def test_an_earlier_run_of_the_same_commit_is_fine(root: Path) -> None:
    api = Api()
    api.cd_runs = [_run(head_branch="2.5.0-rc1")]
    _gate(root, api=api)


def test_a_branch_release_ref_is_refused(tmp_path: Path) -> None:
    write_release_repo(tmp_path, ecbuild_ref="develop")
    with pytest.raises(ReleaseError, match="never a branch"):
        _gate(tmp_path)


def test_a_sha_release_ref_is_checked(tmp_path: Path) -> None:
    write_release_repo(tmp_path, ecbuild_ref=ECBUILD_SHA)
    assert _gate(tmp_path).source_deps[0]["sha"] == ECBUILD_SHA
    write_release_repo(tmp_path, ecbuild_ref="d" * 40)
    with pytest.raises(ReleaseError, match="is not a commit"):
        _gate(tmp_path)


def test_a_workflow_breaking_the_rules_is_refused(tmp_path: Path) -> None:
    broken = SAMPLE_WORKFLOW.read_text().replace("cancel-in-progress: false", "cancel-in-progress: true")
    write_release_repo(tmp_path, workflow=broken)
    with pytest.raises(ReleaseError, match="I3:"):
        _gate(tmp_path)


def test_a_missing_read_url_is_refused(root: Path) -> None:
    inp = _inputs(root, nexus_read_urls={})
    with pytest.raises(ReleaseError, match="no Nexus URL for target"):
        gate.run_gate(inp, api=Api(), sleep=lambda s: None)


def test_the_probe_uses_the_credential_and_keeps_it_out_of_the_outputs(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str | None]] = []

    def probe(base_url: str, filename: str, *, auth: str | None = None) -> str:
        seen.append((base_url, auth))
        return "present"

    monkeypatch.setattr(nexus, "probe", probe)
    inp = _inputs(root, nexus_auth="u:s3cr3t-token")
    result = gate.run_gate(inp, api=Api(), sleep=lambda s: None)
    assert seen == [("https://nexus-test.ecmwf.int/repository/rpm", "u:s3cr3t-token")]
    assert result.legs["release-rpm"][0]["_release"]["state"] == "already-published"
    out = capsys.readouterr().out
    assert "s3cr3t" not in json.dumps(result.outputs()) + repr(inp) + out
    assert "no Nexus credential" not in out


def test_without_a_credential_the_probe_is_anonymous_and_warned(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str | None] = []

    def probe(base_url: str, filename: str, *, auth: str | None = None) -> str:
        seen.append(auth)
        return "absent"

    monkeypatch.setattr(nexus, "probe", probe)
    gate.run_gate(_inputs(root), api=Api(), sleep=lambda s: None)
    assert seen == [""]
    assert "::warning::no Nexus credential ($NEXUS_AUTH): probing anonymously" in capsys.readouterr().out


def test_a_refused_authenticated_probe_refuses_the_release(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def probe(base_url: str, filename: str, *, auth: str | None = None) -> str:
        raise ReleaseError("Nexus refused the authenticated probe")

    monkeypatch.setattr(nexus, "probe", probe)
    with pytest.raises(ReleaseError, match="refused the authenticated probe"):
        gate.run_gate(_inputs(root, nexus_auth="u:p"), api=Api(), sleep=lambda s: None)


def test_a_production_read_url_is_refused(root: Path) -> None:
    inp = _inputs(root, nexus_read_urls={"rocky-9": "https://nexus.ecmwf.int/repository/rpm"})
    with pytest.raises(ReleaseError, match="not a test Nexus host"):
        gate.run_gate(inp, api=Api(), sleep=lambda s: None)


# --- rehearsal ----------------------------------------------------------------------------------------------


def _labelled(head_repo: str = REPO, label: str = "approved-for-cd") -> dict[str, Any]:
    return {
        "action": "labeled",
        "label": {"name": label},
        "pull_request": {"number": 7, "head": {"sha": HEAD, "ref": "feature/x", "repo": {"full_name": head_repo}}},
    }


def _rehearsal(root: Path, api: Api, **over: Any) -> gate.GateResult:
    return _gate(
        root,
        api=api,
        event_name="pull_request",
        ref="refs/pull/7/merge",
        payload=_labelled(),
        workflow_ref=f"{REPO}/.github/workflows/cd-v3.yml@refs/pull/7/merge",
        **over,
    )


def test_a_rehearsal_accepts_the_prs_ci_run_on_its_head(root: Path) -> None:
    pr_run = _run(event="pull_request", head_sha=HEAD, head_branch="feature/x", pull_requests=[{"number": 7}])
    api = Api([[pr_run]])
    result = _rehearsal(root, api)
    out = result.outputs()
    assert (out["mode"], out["publish"], out["sha"], out["head-sha"], out["tag"]) == (
        "rehearsal",
        "false",
        SHA,
        HEAD,
        "",
    )
    assert result.legs["release-rpm"][0]["_release"]["job_key"] == "pkg-rehearsal-rpm-rocky-9-gxx-11-aaaaaaaa"
    # No tag-moved check, no Nexus probe.
    assert not [c for c in api.calls if "cd-v3.yml" in c]


def test_a_rehearsal_matches_the_pr_by_branch_when_github_lists_none(root: Path) -> None:
    pr_run = _run(event="pull_request", head_sha=HEAD, head_branch="feature/x", pull_requests=[])
    _rehearsal(root, Api([[pr_run]]))


def test_a_rehearsal_refuses_another_prs_run(root: Path) -> None:
    other = _run(event="pull_request", head_sha=HEAD, head_branch="other", pull_requests=[{"number": 8}])
    with pytest.raises(ReleaseError, match="within"):
        _rehearsal(root, Api([[other]]), ci_wait_seconds=0)


def test_a_rehearsal_waits_for_the_pr_run(root: Path) -> None:
    running = _run(event="pull_request", head_sha=HEAD, status="queued", conclusion=None, pull_requests=[{"number": 7}])
    done = _run(event="pull_request", head_sha=HEAD, pull_requests=[{"number": 7}])
    clock = Clock()
    _rehearsal(root, Api([[running], [done]]), clock=clock)
    assert clock.sleeps == [60]


def test_a_fork_pr_is_refused(root: Path) -> None:
    with pytest.raises(ReleaseError, match="no rehearsal from a fork"):
        _gate(root, event_name="pull_request", ref="refs/pull/7/merge", payload=_labelled(head_repo="fork/pkg"))


# --- the CLI ------------------------------------------------------------------------------------------------


def test_cli_reads_the_github_environment(root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: Any) -> None:
    out = tmp_path_factory.mktemp("o") / "out"
    event = tmp_path_factory.mktemp("e") / "event.json"
    event.write_text("{}")
    for k, v in {
        "GITHUB_OUTPUT": str(out),
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/tags/2.5.0-rc1",
        "GITHUB_SHA": SHA,
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "CD_KILL_SWITCH": "on",
        "CD_KILL_SWITCH_REASON": "frozen",
        "NEXUS_AUTH": "u:secret",
    }.items():
        monkeypatch.setenv(k, v)
    result = CliRunner().invoke(
        gate.main, ["--manifest", str(root / ".ci" / "manifest.toml"), "--kinds", "release-rpm"]
    )
    assert result.exit_code == 1
    assert "::error::CD kill switch is on: frozen" in result.output
    # Taken out of the environment: no subprocess (gh) inherits it.
    assert "NEXUS_AUTH" not in os.environ


def test_workflow_path_from_the_ref() -> None:
    assert gate._workflow_path(WORKFLOW_REF, REPO) == ".github/workflows/cd-v3.yml"
    with pytest.raises(ReleaseError):
        gate._workflow_path(f"{REPO}/../x@refs/tags/1", REPO)
