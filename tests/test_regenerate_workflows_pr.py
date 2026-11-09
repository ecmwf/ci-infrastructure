# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""regenerate-workflows-pr's PR step opening a new PR, with stub `gh` and `git push`; and its caller's labels."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from conftest import REPO_ROOT, stub_gh

REPO_LABELS: Final = ["downstream-ci-not-needed", "ATS Approval Not Needed", "bug"]


def _pr_step_body() -> str:
    doc: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / "actions" / "regenerate-workflows-pr" / "action.yml").read_text(encoding="utf-8")
    )
    (step,) = (s for s in doc["runs"]["steps"] if s.get("id") == "pr")
    return str(step["run"])


def _open_pr(tmp_path: Path, labels: str, optional_labels: str = "") -> tuple[list[str], str]:
    """Open a PR for a drifted workflow; returns the `--label` values of `gh pr create` and the output."""
    work = tmp_path / "work"
    (work / ".github" / "workflows").mkdir(parents=True)
    wf = work / ".github" / "workflows" / "x.yml"
    wf.write_text("on: push\n")
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    wf.write_text("on: pull_request\n")

    (tmp_path / "labels.txt").write_text("".join(f"{label}\n" for label in REPO_LABELS))
    log = tmp_path / "pr-create.log"
    bindir = stub_gh(
        tmp_path,
        f'case "$1 $2" in\n'
        f'  "label list") cat {tmp_path}/labels.txt ;;\n'
        f'  "api users/"*) echo 1 ;;\n'
        f'  "pr create") shift 2; printf "%s\\n" "$@" > {log}; echo https://github.com/o/r/pull/1 ;;\n'
        f'  "api "*) exit 1 ;;\n'
        f"esac",
    )
    real_git = shutil.which("git")
    (bindir / "git").write_text(f'#!/bin/sh\nfor a; do [ "$a" = push ] && exit 0; done\nexec {real_git} "$@"\n')
    (bindir / "git").chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(tmp_path / "outputs.txt"),
        "GITHUB_REPOSITORY": "o/r",
        "GH_TOKEN": "t",
        "APP_SLUG": "bot",
        "BRANCH": "ci/regenerate-workflows",
        "BASE": "main",
        "LABELS": labels,
        "OPTIONAL_LABELS": optional_labels,
        "TITLE": "",
        "MODE": "pr",
        "DIRECT_PUSH": "false",
        "RUN_URL": "https://example.invalid/run",
    }
    proc = subprocess.run(
        ["bash", "-c", _pr_step_body()], cwd=work, env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    args = log.read_text().splitlines()
    return [args[i + 1] for i, a in enumerate(args) if a == "--label"], proc.stdout + proc.stderr


def test_labels_match_case_insensitively_with_the_repos_spelling(tmp_path: Path) -> None:
    labels, out = _open_pr(tmp_path, " ats approval not needed ,BUG")
    assert labels == ["ATS Approval Not Needed", "bug"]
    assert "::warning" not in out


def test_a_missing_label_warns(tmp_path: Path) -> None:
    labels, out = _open_pr(tmp_path, "bug,typo-label")
    assert labels == ["bug"]
    assert "::warning title=Regenerate workflows::o/r has no label 'typo-label'" in out


def test_a_missing_optional_label_is_left_out_quietly(tmp_path: Path) -> None:
    labels, out = _open_pr(tmp_path, "", "downstream-ci-not-needed,hpc-ci-not-needed")
    assert labels == ["downstream-ci-not-needed"]
    assert "::warning" not in out
    assert "::notice title=Regenerate workflows::o/r has no label 'hpc-ci-not-needed'" in out


def test_a_label_given_twice_is_applied_once(tmp_path: Path) -> None:
    labels, _ = _open_pr(tmp_path, "Bug", "bug,downstream-ci-not-needed")
    assert labels == ["bug", "downstream-ci-not-needed"]


def test_no_label_at_all(tmp_path: Path) -> None:
    assert _open_pr(tmp_path, "", "hpc-ci-not-needed")[0] == []


# --- regenerate-workflows.yml: only its own default labels are optional ----------------------------

DEFAULT: Final = "downstream-ci-not-needed,hpc-ci-not-needed"


def _call() -> tuple[dict[str, Any], dict[str, str]]:
    doc: dict[Any, Any] = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "regenerate-workflows.yml").read_text(encoding="utf-8")
    )
    inputs = doc[True]["workflow_call"]["inputs"]
    (step,) = (s for s in doc["jobs"]["regenerate"]["steps"] if "regenerate-workflows-pr" in str(s.get("uses", "")))
    return inputs, step["with"]


def _evaluate(expr: str, labels: str, add_labels: str) -> str:
    """The two expression forms the step uses, evaluated for these inputs."""
    is_default = labels.lower() == DEFAULT.lower()
    if expr == f"${{{{ format('{{0}},{{1}}', inputs.labels != '{DEFAULT}' && inputs.labels || '', inputs.add-labels) }}}}":
        return f"{'' if is_default else labels},{add_labels}"
    if expr == f"${{{{ inputs.labels == '{DEFAULT}' && inputs.labels || '' }}}}":
        return labels if is_default else ""
    raise AssertionError(f"unexpected expression {expr!r}")


def test_the_workflow_compares_against_its_own_default() -> None:
    inputs, _ = _call()
    assert inputs["labels"]["default"] == DEFAULT


@pytest.mark.parametrize(
    ("labels", "add_labels", "explicit", "optional"),
    [
        (DEFAULT, "", ",", DEFAULT),
        (DEFAULT, "ATS Approval not needed", ",ATS Approval not needed", DEFAULT),
        ("my-label", "x", "my-label,x", ""),
        ("", "x", ",x", ""),
    ],
)
def test_only_the_default_labels_are_optional(labels: str, add_labels: str, explicit: str, optional: str) -> None:
    _, with_ = _call()
    assert _evaluate(with_["labels"], labels, add_labels) == explicit
    assert _evaluate(with_["optional-labels"], labels, add_labels) == optional
