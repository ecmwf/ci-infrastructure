# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""check_ci_approval: which workflows need the gate, direct needs, allowlists and allow-unsafe-pr-checkout."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

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


def _single_job(tmp_path: Path, trigger: str, runs_on: str) -> Path:
    return write_wf(
        tmp_path,
        "wf.yml",
        f"""
        on: {trigger}
        jobs:
          build:
            runs-on: {runs_on}
            steps: [{{run: make}}]
        """,
    )


@pytest.mark.parametrize(
    ("trigger", "runs_on", "needle"),
    [
        ("push", "arc-runner-normal", None),
        ("pull_request", "ubuntu-latest", None),
        ("pull_request", "ubuntu-slim", None),
        ("pull_request_target", "ubuntu-latest", "no job uses"),
        ("pull_request", "arc-runner-normal", "non-GitHub-hosted"),
        ("pull_request", "${{ matrix.runs-on }}", ""),
    ],
    ids=["push", "hosted-pr", "slim-is-hosted", "prt", "self-hosted-pr", "expression-runner"],
)
def test_which_workflows_need_a_gate(tmp_path: Path, trigger: str, runs_on: str, needle: str | None) -> None:
    problems = check(_single_job(tmp_path, trigger, runs_on))
    if needle is None:
        assert problems == []
    else:
        assert problems and all(needle in p for p in problems)


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


def test_gate_on_self_hosted_is_not_exempt(tmp_path: Path) -> None:
    for trigger in ("pull_request", "pull_request_target"):
        wf = write_wf(
            tmp_path,
            f"{trigger}.yml",
            f"""
            on: {trigger}
            jobs:
              ci-approval:
                runs-on: arc-runner-normal
                steps:
                  - uses: ecmwf/ci-infrastructure/actions/require-ci-approval@main
              build:
                needs: [ci-approval]
                runs-on: arc-runner-normal
                steps: [{{run: make}}]
            """,
        )
        (problem,) = check(wf)
        assert "job 'ci-approval' must list 'ci-approval'" in problem


def test_transitive_needs_is_rejected(tmp_path: Path) -> None:
    """One edge moved later would silently ungate `build`."""
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


@pytest.mark.parametrize(
    "allowlist", ["exempt:\n  - workflow: wf.yml\n", "exempt:\n  - workflow: wf.yml\n    reason: API-only\n"]
)
def test_allowlist_whole_workflow(tmp_path: Path, allowlist: str) -> None:
    wf = _single_job(tmp_path, "pull_request_target", "ubuntu-latest")
    write_allowlist(tmp_path, allowlist)
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


@pytest.mark.parametrize(("trigger", "gated"), [("pull_request", False), ("pull_request_target", True)])
def test_reusable_workflow_job(tmp_path: Path, trigger: str, gated: bool) -> None:
    """A `uses:` job names no runner; the called workflow is checked on its own."""
    wf = write_wf(
        tmp_path,
        "caller.yml",
        f"""
        on: {trigger}
        jobs:
          delegate:
            uses: ./.github/workflows/other.yml
        """,
    )
    assert bool(check(wf)) is gated


def _write_manifest(tmp_path: Path, visibility: str) -> None:
    d = tmp_path / ".ci"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.toml").write_text(f'[package]\nname = "x"\nvisibility = "{visibility}"\n', encoding="utf-8")


@pytest.mark.parametrize(("visibility", "gated"), [("private", False), ("public", True)])
def test_manifest_visibility(tmp_path: Path, visibility: str, gated: bool) -> None:
    """Forking a private/internal repo already needs access."""
    wf = _single_job(tmp_path, "pull_request_target", "arc-runner-normal")
    _write_manifest(tmp_path, visibility)
    assert bool(check(wf)) is gated


def _unsafe_wf(tmp_path: Path, needs: str = "", value: str = "'true'") -> Path:
    return write_wf(
        tmp_path,
        "ci.yml",
        f"""
        on:
          pull_request:
            types: [opened, synchronize]
        jobs:
        @GATE@
          build:
            runs-on: ubuntu-latest
        {needs}
            steps:
              - uses: ecmwf/ci-infrastructure/actions/checkout-under-test@main
                with:
                  allow-unsafe-pr-checkout: {value}
        """,
    )


def test_unsafe_checkout_behind_the_gate_is_fine(tmp_path: Path) -> None:
    assert check(_unsafe_wf(tmp_path, needs="    needs: [ci-approval]")) == []


def test_unsafe_checkout_without_the_gate_is_rejected(tmp_path: Path) -> None:
    """Hosted `pull_request` needs no gate, but the opt-in goes live under pull_request_target."""
    (problem,) = check(_unsafe_wf(tmp_path))
    assert "allow-unsafe-pr-checkout" in problem
    assert "'build'" in problem


@pytest.mark.parametrize(("value", "problems"), [("true", 1), ("'false'", 0)])
def test_unsafe_checkout_value(tmp_path: Path, value: str, problems: int) -> None:
    assert len(check(_unsafe_wf(tmp_path, value=value))) == problems


def test_unsafe_checkout_respects_a_job_exemption(tmp_path: Path) -> None:
    write_allowlist(tmp_path, "exempt:\n  - workflow: ci.yml\n    jobs: [build]\n")
    assert check(_unsafe_wf(tmp_path)) == []


def test_unsafe_checkout_under_workflow_run_is_exempt(tmp_path: Path) -> None:
    """require-ci-approval is inert under workflow_run."""
    wf = write_wf(
        tmp_path,
        "trigger-downstream.yml",
        """
        on:
          workflow_run:
            workflows: [CI]
            types: [completed]
        jobs:
          validate:
            runs-on: ubuntu-slim
            steps:
              - uses: actions/checkout@v6
                with:
                  allow-unsafe-pr-checkout: true
        """,
    )
    assert check(wf) == []


def test_both_rules_fire_independently(tmp_path: Path) -> None:
    wf = write_wf(
        tmp_path,
        "ci.yml",
        """
        on:
          pull_request_target:
            types: [opened, synchronize]
        jobs:
        @GATE@
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: ecmwf/ci-infrastructure/actions/checkout-under-test@main
                with:
                  allow-unsafe-pr-checkout: 'true'
        """,
    )
    problems = check(wf)
    assert len(problems) == 2, problems
    assert any("allow-unsafe-pr-checkout" in p for p in problems)
    assert any("must list 'ci-approval' in `needs:`" in p for p in problems)


_OPTIONAL_DEPS = {"boto3", "botocore", "pydantic", "jinja2", "troika"}


def test_the_linter_imports_nothing_optional() -> None:
    """Checked on the source, so it holds where the optional packages happen to be installed."""
    src = Path(check.__globals__["__file__"]).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & (_OPTIONAL_DEPS | {"ci_infrastructure"}), imported
