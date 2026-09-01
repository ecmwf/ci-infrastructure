# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ci_infrastructure.check_ci_approval.

Covers both trigger shapes that need the gate, the cases that must stay quiet
(GitHub-hosted `pull_request`, `push`), the transitive-needs hole the checker
exists to close, and both allowlist granularities.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from ci_infrastructure.check_ci_approval import check

GATE = """  ci-approval:
    runs-on: ubuntu-slim
    steps:
      - uses: ecmwf/ci-infrastructure/actions/require-ci-approval@main
"""


def write_wf(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(textwrap.dedent(body).replace("@GATE@\n", GATE), encoding="utf-8")
    return p


def write_allowlist(tmp_path: Path, body: str) -> None:
    d = tmp_path / ".github"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ci-approval-allowlist.yml").write_text(textwrap.dedent(body), encoding="utf-8")


def test_push_only_is_ignored(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "push.yml",
        """
        on:
          push:
            branches: [main]
        jobs:
          build:
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    assert check(wf) == []


def test_pull_request_on_github_hosted_is_ignored(tmp_path: Path) -> None:
    """Forks get no secrets and GitHub's own VM — nothing of ours is exposed."""
    wf = write_wf(
        tmp_path,
        "hosted.yml",
        """
        on: pull_request
        jobs:
          build:
            runs-on: ubuntu-latest
            steps: [{run: make}]
        """,
    )
    assert check(wf) == []


def test_pull_request_target_needs_a_gate(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "prt.yml",
        """
        on: pull_request_target
        jobs:
          label:
            runs-on: ubuntu-latest
            steps: [{run: gh pr edit}]
        """,
    )
    (problem,) = check(wf)
    assert "pull_request_target" in problem
    assert "no job uses" in problem


def test_pull_request_on_self_hosted_needs_a_gate(tmp_path: Path) -> None:
    """The ECMWF-hardware case: no secrets, but our runner runs their code."""
    wf = write_wf(
        tmp_path,
        "arc.yml",
        """
        on: pull_request
        jobs:
          build:
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    (problem,) = check(wf)
    assert "non-GitHub-hosted" in problem


def test_expression_runner_is_not_assumed_hosted(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "matrix.yml",
        """
        on: pull_request
        jobs:
          build:
            runs-on: ${{ matrix.runs-on }}
            steps: [{run: make}]
        """,
    )
    assert check(wf) != []


def test_gated_workflow_passes(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "ok.yml",
        """
        on: pull_request_target
        jobs:
        @GATE@
          build:
            needs: [ci-approval]
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    assert check(wf) == []


def test_transitive_needs_is_rejected(tmp_path: Path) -> None:
    """`build` waits on `resolve` which waits on the gate. Not good enough: one
    edge moved later silently ungates build."""
    wf = write_wf(
        tmp_path,
        "transitive.yml",
        """
        on: pull_request_target
        jobs:
        @GATE@
          resolve:
            needs: ci-approval
            runs-on: arc-runner-normal
            steps: [{run: resolve}]
          build:
            needs: resolve
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    (problem,) = check(wf)
    assert "'build'" in problem


def test_allowlist_whole_workflow(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "bot.yml",
        """
        on: pull_request_target
        jobs:
          label:
            runs-on: ubuntu-latest
            steps: [{run: gh pr edit}]
        """,
    )
    write_allowlist(tmp_path, "exempt:\n  - workflow: bot.yml\n")
    assert check(wf) == []


def test_allowlist_single_job(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "mixed.yml",
        """
        on: pull_request_target
        jobs:
        @GATE@
          label:
            runs-on: ubuntu-latest
            steps: [{run: gh pr edit}]
          build:
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    write_allowlist(tmp_path, "exempt:\n  - workflow: mixed.yml\n    jobs: [label]\n")
    (problem,) = check(wf)
    assert "'build'" in problem


def test_allowlist_reason_is_optional_and_ignored(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "bot.yml",
        """
        on: pull_request_target
        jobs:
          label:
            runs-on: ubuntu-latest
            steps: [{run: gh pr edit}]
        """,
    )
    write_allowlist(tmp_path, "exempt:\n  - workflow: bot.yml\n    reason: API-only\n")
    assert check(wf) == []


def test_ubuntu_slim_counts_as_hosted(tmp_path: Path) -> None:
    """Despite the name it is a GitHub-hosted larger runner, not an ARC one."""
    wf = write_wf(
        tmp_path,
        "slim.yml",
        """
        on: pull_request
        jobs:
          lint:
            runs-on: ubuntu-slim
            steps: [{run: pre-commit run}]
        """,
    )
    assert check(wf) == []


def test_reusable_workflow_job_is_not_assumed_self_hosted(tmp_path: Path) -> None:
    """A `uses:` job names no runner; the called workflow is checked on its own."""
    wf = write_wf(
        tmp_path,
        "caller.yml",
        """
        on: pull_request
        jobs:
          delegate:
            uses: ./.github/workflows/other.yml
        """,
    )
    assert check(wf) == []


def test_reusable_workflow_job_still_gated_under_pull_request_target(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "bot.yml",
        """
        on: pull_request_target
        jobs:
          delegate:
            uses: ./.github/workflows/other.yml
        """,
    )
    assert check(wf) != []


def _write_manifest(tmp_path: Path, visibility: str) -> None:
    d = tmp_path / ".ci"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.toml").write_text(f'[package]\nname = "x"\nvisibility = "{visibility}"\n', encoding="utf-8")


def test_private_repo_is_skipped(tmp_path: Path) -> None:
    """Forking a private/internal repo already needs access."""
    wf = write_wf(
        tmp_path,
        "arc.yml",
        """
        on: pull_request_target
        jobs:
          build:
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    _write_manifest(tmp_path, "private")
    assert check(wf) == []


def test_public_manifest_stays_strict(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "arc.yml",
        """
        on: pull_request_target
        jobs:
          build:
            runs-on: arc-runner-normal
            steps: [{run: make}]
        """,
    )
    _write_manifest(tmp_path, "public")
    assert check(wf) != []
