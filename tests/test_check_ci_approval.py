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
              - uses: actions/checkout@v6
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
              - uses: actions/checkout@v6
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


FORK_CI_YML = """
on:
  pull_request:
  workflow_call:
jobs:
  ci-approval:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-slim
    steps:
      - uses: ecmwf/ci-infrastructure/actions/require-ci-approval@main
  build:
    needs: ci-approval
    runs-on: arc-runner-normal
    steps:
      - uses: ecmwf/ci-infrastructure/actions/checkout-under-test@main
        with:
          allow-unsafe-pr-checkout: ${{ github.event_name == 'pull_request_target' }}
      - uses: actions/checkout@v6
        with:
          repository: ecmwf/ecbuild
"""

FORK_CALLER = """
on:
  pull_request_target:
    types: [labeled, synchronize, reopened]
jobs:
  ci:
    uses: ./.github/workflows/ci.yml
    secrets: inherit
"""


SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
OPT_IN = "allow-unsafe-pr-checkout: ${{ github.event_name == 'pull_request_target' }}"


def _opt_in(tmp_path: Path, opted: bool = True) -> None:
    (tmp_path / ".ci").mkdir(exist_ok=True)
    (tmp_path / ".ci" / "manifest.toml").write_text(
        f'[package]\nname = "a"\nvisibility = "public"\nfork-pull-requests = {str(opted).lower()}\n', encoding="utf-8"
    )


def test_a_fork_ci_yml_and_its_caller_pass(tmp_path: Path) -> None:
    _opt_in(tmp_path)
    ci = write_wf(tmp_path, "ci.yml", FORK_CI_YML)
    caller = write_wf(tmp_path, "ci-fork.yml", FORK_CALLER)
    assert check(ci) == []
    assert check(caller) == []


def test_the_caller_is_ungated_when_ci_yml_is(tmp_path: Path) -> None:
    _opt_in(tmp_path)
    write_wf(tmp_path, "ci.yml", FORK_CI_YML.replace("    needs: ci-approval\n", ""))
    caller = write_wf(tmp_path, "ci-fork.yml", FORK_CALLER)
    assert any("no job uses" in p for p in check(caller))


def test_a_fork_caller_without_the_opt_in_fails(tmp_path: Path) -> None:
    _opt_in(tmp_path, opted=False)
    write_wf(tmp_path, "ci.yml", FORK_CI_YML)
    caller = write_wf(tmp_path, "ci-fork.yml", FORK_CALLER)
    assert any("does not set fork-pull-requests = true" in p for p in check(caller))


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("  workflow_call:\n", "", "no `workflow_call`"),
        (f"    if: github.event_name != 'pull_request' || {SAME_REPO}\n", "", "must skip a fork's pull_request run"),
        (
            f"      - uses: ecmwf/ci-infrastructure/actions/checkout-under-test@main\n"
            f"        with:\n          {OPT_IN}\n",
            "      - uses: actions/checkout@v6\n",
            "use ecmwf/ci-infrastructure/actions/checkout-under-test",
        ),
        (OPT_IN, "allow-unsafe-pr-checkout: true", "must pass allow-unsafe-pr-checkout"),
        ("  ci-approval:\n", "  gate:\n", "must be the job 'ci-approval'"),
    ],
    ids=["no-workflow-call", "no-fork-skip", "plain-checkout", "unconditional-opt-in", "gate-renamed"],
)
def test_ci_yml_must_be_fit_for_the_fork_caller(tmp_path: Path, old: str, new: str, needle: str) -> None:
    _opt_in(tmp_path)
    assert old in FORK_CI_YML
    body = FORK_CI_YML.replace(old, new).replace(
        "needs: ci-approval", "needs: gate" if new == "  gate:\n" else "needs: ci-approval"
    )
    problems = check(write_wf(tmp_path, "ci.yml", body))
    assert any(needle in p for p in problems), problems


def test_without_the_opt_in_ci_yml_is_not_held_to_it(tmp_path: Path) -> None:
    _opt_in(tmp_path, opted=False)
    body = FORK_CI_YML.replace("  workflow_call:\n", "")
    assert check(write_wf(tmp_path, "ci.yml", body)) == []


def test_an_expression_for_allow_unsafe_pr_checkout_needs_the_gate(tmp_path: Path) -> None:
    _opt_in(tmp_path, opted=False)
    body = FORK_CI_YML.replace("    needs: ci-approval\n", "")
    assert any("sets allow-unsafe-pr-checkout" in p for p in check(write_wf(tmp_path, "ci.yml", body)))


def test_a_workflow_called_under_pull_request_target_is_held_to_it(tmp_path: Path) -> None:
    """Its jobs run with the caller's secrets, even on GitHub-hosted runners."""
    ungated = """
    on: [pull_request, workflow_call]
    jobs:
    @GATE@
      lint:
        runs-on: ubuntu-latest
        steps: [{run: make lint}]
    """
    callee = write_wf(tmp_path, "ci.yml", ungated)
    assert check(callee) == []
    caller = write_wf(tmp_path, "prt.yml", FORK_CALLER)
    assert check(callee) == [
        f"{callee}: job 'lint' must list 'ci-approval' in `needs:` "
        "(pull_request_target (through prt.yml) runs with this repository's secrets)"
    ]
    assert any("no job uses" in p for p in check(caller))


def test_the_caller_triggers_reach_through_every_call(tmp_path: Path) -> None:
    write_wf(tmp_path, "prt.yml", FORK_CALLER)
    write_wf(tmp_path, "ci.yml", "on: workflow_call\njobs:\n  inner:\n    uses: ./.github/workflows/inner.yml\n")
    inner = write_wf(
        tmp_path,
        "inner.yml",
        "on: workflow_call\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: [{run: make}]\n",
    )
    assert any("pull_request_target (through prt.yml)" in p for p in check(inner))


def test_a_cycle_of_calls_terminates(tmp_path: Path) -> None:
    a = write_wf(tmp_path, "a.yml", "on: pull_request_target\njobs:\n  b:\n    uses: ./.github/workflows/b.yml\n")
    write_wf(tmp_path, "b.yml", "on: workflow_call\njobs:\n  a:\n    uses: ./.github/workflows/a.yml\n")
    assert any("no job uses" in p for p in check(a))


def test_a_workflow_whose_jobs_are_all_exempt_needs_no_gate(tmp_path: Path) -> None:
    write_allowlist(tmp_path, "exempt:\n  - workflow: wf.yml\n    jobs: [build]\n")
    assert check(_single_job(tmp_path, "pull_request_target", "ubuntu-latest")) == []


LABEL_GATE = """
on:
  pull_request_target:
    types: [opened, labeled]
jobs:
  gate:
    runs-on: ubuntu-slim
    steps:
      - uses: ecmwf/ci-infrastructure/actions/require-label-decision@main
        with:
          context: downstream-ci-label
"""


def test_a_payload_only_job_needs_no_gate(tmp_path: Path) -> None:
    assert check(write_wf(tmp_path, "gate.yml", LABEL_GATE)) == []


@pytest.mark.parametrize(
    "extra",
    ["      - uses: actions/checkout@v6\n", "      - run: make\n"],
    ids=["checkout", "run"],
)
def test_a_payload_only_job_with_anything_else_needs_the_gate(tmp_path: Path, extra: str) -> None:
    problems = check(write_wf(tmp_path, "gate.yml", LABEL_GATE + extra))
    assert any("no job uses" in p for p in problems)
