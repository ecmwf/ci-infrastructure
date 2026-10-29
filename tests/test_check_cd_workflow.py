# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""check-cd-workflow on samples/cd/cd.yml, and one broken copy per rule."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from release_fixtures import SAMPLE_WORKFLOW, write_release_repo

from ci_infrastructure import check_cd_workflow, check_ci_approval

ALLOW = ("feature/cd-v3",)
Doc = dict[Any, Any]


def _sample() -> Doc:
    doc = yaml.safe_load(SAMPLE_WORKFLOW.read_text())
    assert isinstance(doc, dict)
    return doc


def _problems(doc: Doc, allow_refs: tuple[str, ...] = ALLOW) -> list[str]:
    return check_cd_workflow.check_text(yaml.safe_dump(doc, sort_keys=False), allow_refs=allow_refs)


def test_the_sample_passes() -> None:
    assert check_cd_workflow.check(SAMPLE_WORKFLOW, allow_refs=ALLOW) == []


def test_the_sample_passes_check_ci_approval_as_a_public_repo(tmp_path: Path) -> None:
    """eckit-cd-test is private, where check-ci-approval skips; a public consumer must pass it too."""
    write_release_repo(tmp_path, visibility="public")
    assert check_ci_approval.check(tmp_path / ".github" / "workflows" / "cd-v3.yml") == []


def _steps(doc: Doc, job: str) -> list[dict[str, Any]]:
    steps = doc["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


def _set(path: str, value: Any) -> Callable[[Doc], None]:
    def mutate(doc: Doc) -> None:
        *head, last = path.split(".")
        node = doc
        for key in head:
            node = node[True if key == "on" and True in node else key]
        if value is _DELETE:
            del node[last]
        else:
            node[last] = value

    return mutate


_DELETE = object()


def _upload_step(doc: Doc) -> dict[str, Any]:
    return next(s for s in _steps(doc, "publish-nexus") if "upload-nexus" in str(s.get("uses")))


def _add_run_step(job: str, run: str) -> Callable[[Doc], None]:
    return lambda doc: _steps(doc, job).append({"run": run})


_VIOLATIONS: list[tuple[str, Callable[[Doc], None], str]] = [
    (
        "pull_request_target",
        lambda d: d[True].update({"pull_request_target": None}),
        "I1: trigger 'pull_request_target'",
    ),
    ("workflow_run", lambda d: d[True].update({"workflow_run": {"workflows": ["CI"]}}), "I1: trigger 'workflow_run'"),
    ("push branches", lambda d: d[True]["push"].update({"branches": ["main"]}), "I1: `push` must name `tags`"),
    ("pr opened", lambda d: d[True]["pull_request"].update({"types": ["opened", "labeled"]}), "I1: `pull_request`"),
    ("write permissions", _set("permissions", {"contents": "write"}), "I2:"),
    ("no permissions", _set("permissions", _DELETE), "I2:"),
    ("no concurrency", _set("concurrency", _DELETE), "I3: a `concurrency` group"),
    ("cancel in progress", _set("concurrency.cancel-in-progress", True), "I3: `cancel-in-progress: false`"),
    ("secrets inherit", _set("jobs.gate.secrets", "inherit"), "I4: job 'gate': no `secrets: inherit`"),
    ("workflow env", _set("env", {"X": "1"}), "I4: no workflow-level `env`"),
    ("sccache", _set("jobs.build-rpm.env", {"SCCACHE_BUCKET": "${{ secrets.SCCACHE_BUCKET }}"}), "I4: no CI cache"),
    ("s3", _set("jobs.gate.env", {"A": "${{ secrets.ARTIFACT_S3_BUCKET }}"}), "I4: no CI cache"),
    (
        "secret in a build job",
        _set("jobs.build-rpm.env", {"T": "${{ secrets.NEXUS_TEST_REPO_UPLOAD_TOKEN }}"}),
        "I5: job 'build-rpm' names secrets ['NEXUS_TEST_REPO_UPLOAD_TOKEN'] but is not a publish job",
    ),
    (
        "URL secret in a build job",
        _set("jobs.build-rpm.env", {"U": "${{ secrets.NEXUS_TEST_REPO_URL_ROCKY_9 }}"}),
        "I5: job 'build-rpm' names secrets ['NEXUS_TEST_REPO_URL_ROCKY_9']",
    ),
    (
        "credential secret in the gate job",
        lambda d: _steps(d, "gate")[1]["env"].update({"T": "${{ secrets.NEXUS_TEST_REPO_UPLOAD_TOKEN }}"}),
        "I5: job 'gate' names secrets ['NEXUS_TEST_REPO_UPLOAD_TOKEN']",
    ),
    (
        "toJSON(secrets) in the gate job",
        lambda d: _steps(d, "gate")[1]["env"].update({"X": "${{ toJSON(secrets) }}"}),
        "I5: job 'gate' reads `secrets` without a literal name",
    ),
    (
        "toJSON(secrets) in a build job",
        _set("jobs.build-rpm.env", {"X": "${{ toJSON(secrets) }}"}),
        "I5: job 'build-rpm' reads `secrets` without a literal name",
    ),
    (
        "dynamic secret name in a build job",
        _set("jobs.build-rpm.env", {"X": "${{ secrets[inputs.name] }}"}),
        "I5: job 'build-rpm' reads `secrets` without a literal name",
    ),
    ("run in publish job", _add_run_step("publish-nexus", "echo hi"), "I6: publish job 'publish-nexus' has a `run:`"),
    (
        "checkout in publish job",
        lambda d: _steps(d, "publish-nexus").insert(0, {"uses": "actions/checkout@" + "a" * 40}),
        "I6: publish job 'publish-nexus' uses 'actions/checkout",
    ),
    (
        "build in publish job",
        lambda d: _steps(d, "publish-nexus").insert(
            0, {"uses": "ecmwf/ci-infrastructure/actions/run-job-script@feature/cd-v3"}
        ),
        "uses 'ecmwf/ci-infrastructure/actions/run-job-script",
    ),
    *[
        (
            f"{key} in a publish job step",
            lambda d, key=key: _upload_step(d)["env"].update({key: "x"}),
            f"I6: publish job 'publish-nexus' step 3: `env` sets ['{key}']",
        )
        for key in (
            "BASH_ENV",
            "ENV",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "LD_AUDIT",
            "PATH",
            "NODE_OPTIONS",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONHOME",
            "GIT_SSH_COMMAND",
            "PERL5OPT",
            "RUBYOPT",
        )
    ],
    (
        "startup env on a publish job",
        _set("jobs.publish-nexus.env", {"LD_PRELOAD": "/tmp/x.so"}),
        "I6: publish job 'publish-nexus': `env` sets ['LD_PRELOAD']",
    ),
    (
        "expression env on a publish job",
        _set("jobs.publish-nexus.env", "${{ fromJSON(needs.gate.outputs.json) }}"),
        "I6: publish job 'publish-nexus': `env` must be a literal mapping",
    ),
    *[
        (
            f"download-artifact {key} in a publish job",
            lambda d, key=key: _steps(d, "publish-nexus")[0]["with"].update({key: "x"}),
            f"I6: publish job 'publish-nexus' step 1: download-artifact ['{key}']",
        )
        for key in ("run-id", "repository", "github-token")
    ],
    (
        "hosted runner",
        _set("jobs.publish-nexus.runs-on", "ubuntu-latest"),
        "I6: publish job 'publish-nexus' runs-on",
    ),
    (
        "foreign container",
        _set("jobs.publish-nexus.container", {"image": "docker.io/ubuntu:24.04"}),
        "a container must come from eccr.ecmwf.int/",
    ),
    (
        "publish from a build job",
        lambda d: _steps(d, "build-rpm").append(dict(_upload_step(d))),
        "I6: publish job 'build-rpm' uses 'actions/checkout",
    ),
    (
        "publish job misses a build job",
        _set("jobs.publish-nexus.needs", ["ci-approval", "gate", "build-rpm"]),
        "I7: publish job 'publish-nexus' must need every build and test job; missing ['itest-rpm']",
    ),
    (
        "plain if on a publish job",
        _set("jobs.publish-nexus.if", "needs.gate.outputs.publish == 'true'"),
        "I7: publish job 'publish-nexus' must run `if:",
    ),
    (
        "expression in run",
        _add_run_step("gate", "echo ${{ github.event.pull_request.title }}"),
        "I8: job 'gate' step 3",
    ),
    (
        "unpinned third-party action",
        lambda d: _steps(d, "gate").__setitem__(0, {"uses": "actions/checkout@v6"}),
        "I9: job 'gate': actions/checkout@v6 must be pinned",
    ),
    (
        "two ci-infrastructure refs",
        lambda d: _steps(d, "gate")[1].update({"uses": "ecmwf/ci-infrastructure/actions/release-gate@" + "b" * 40}),
        "I9: every ci-infrastructure action must use one ref",
    ),
    (
        "transitive approval",
        _set("jobs.itest-rpm.needs", ["gate", "build-rpm"]),
        "I10: job 'itest-rpm' must list 'ci-approval' in `needs:` directly",
    ),
    (
        "transitive gate",
        _set("jobs.record.needs", ["ci-approval", "build-rpm", "itest-rpm", "publish-nexus"]),
        "I10: job 'record' must list 'gate' in `needs:` directly",
    ),
    (
        "no overwrite",
        lambda d: _steps(d, "build-rpm")[3]["with"].pop("overwrite"),
        "job 'build-rpm': actions/upload-artifact must set `overwrite: true`",
    ),
]


@pytest.mark.parametrize(("mutate", "expected"), [v[1:] for v in _VIOLATIONS], ids=[v[0] for v in _VIOLATIONS])
def test_each_rule_catches_its_violation(mutate: Callable[[Doc], None], expected: str) -> None:
    doc = copy.deepcopy(_sample())
    mutate(doc)
    problems = _problems(doc)
    assert any(expected in p for p in problems), problems


def test_a_branch_ref_needs_allow_ref() -> None:
    problems = _problems(_sample(), allow_refs=())
    assert problems and all("I9:" in p and "@feature/cd-v3 must be pinned" in p for p in problems)


def test_the_upload_nexus_credential_is_a_secret_of_the_publish_job() -> None:
    assert _upload_step(_sample())["env"] == {"NEXUS_AUTH": "${{ secrets.NEXUS_TEST_REPO_UPLOAD_TOKEN }}"}


def test_a_publish_job_needs_no_environment() -> None:
    doc = _sample()
    assert "environment" not in doc["jobs"]["publish-nexus"]
    assert _problems(doc) == []


def test_an_environment_is_ignored() -> None:
    doc = _sample()
    doc["jobs"]["publish-nexus"]["environment"] = "anything"
    doc["jobs"]["itest-rpm"]["environment"] = "anything"
    assert _problems(doc) == []


def test_a_job_with_an_environment_is_no_publish_job() -> None:
    doc = _sample()
    doc["jobs"]["build-rpm"]["environment"] = "release"
    doc["jobs"]["build-rpm"]["env"] = {"T": "${{ secrets.NEXUS_TEST_REPO_UPLOAD_TOKEN }}"}
    assert any("I5: job 'build-rpm' names secrets" in p for p in _problems(doc))


def test_the_nexus_url_secret_may_be_read_by_the_gate() -> None:
    doc = _sample()
    gate_with = _steps(doc, "gate")[1]["with"]
    assert "secrets.NEXUS_TEST_REPO_URL_ROCKY_9" in gate_with["nexus-read-urls"]
    gate_with["nexus-read-urls"] = '{"rocky-8": "${{ secrets.NEXUS_TEST_REPO_URL_ROCKY_8 }}"}'
    assert _problems(doc) == []


def test_the_url_exception_is_for_the_release_gate_action_not_the_job_name() -> None:
    doc = _sample()
    _steps(doc, "itest-rpm")[0]["with"]["name"] = "${{ secrets.NEXUS_TEST_REPO_URL_ROCKY_9 }}"
    assert any("I5: job 'itest-rpm' names secrets ['NEXUS_TEST_REPO_URL_ROCKY_9']" in p for p in _problems(doc))


def test_cli_exit_codes(tmp_path: Path) -> None:
    ok = CliRunner().invoke(check_cd_workflow.main, ["--allow-ref", "feature/cd-v3", str(SAMPLE_WORKFLOW)])
    assert ok.exit_code == 0, ok.output
    bad = tmp_path / "cd-v3.yml"
    bad.write_text(SAMPLE_WORKFLOW.read_text().replace("cancel-in-progress: false", "cancel-in-progress: true"))
    result = CliRunner().invoke(check_cd_workflow.main, ["--allow-ref", "feature/cd-v3", str(bad)])
    assert result.exit_code == 1
    assert "I3:" in result.output


def test_not_a_workflow() -> None:
    assert check_cd_workflow.check_text("[1, 2]") == ["<workflow>: not a workflow with jobs"]
    assert "cannot parse" in check_cd_workflow.check_text("a: [")[0]
