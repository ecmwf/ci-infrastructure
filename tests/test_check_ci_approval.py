# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ci_infrastructure.check_ci_approval.

Covers both trigger shapes that need the gate, the cases that must stay quiet
(GitHub-hosted `pull_request`, `push`), the transitive-needs hole the checker
exists to close, both allowlist granularities, and the allow-unsafe-pr-checkout
rule that is enforced independently of all of the above.
"""

from __future__ import annotations

import ast
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


# --- allow-unsafe-pr-checkout ------------------------------------------------


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
    """`pull_request` on ubuntu-latest: the gate rule stays quiet, this one does not.

    That gap is the point. Nothing of ours is exposed by the trigger alone, so
    _gate_reason returns None -- but the step still opts in to fetching a fork's
    code, and under a later pull_request_target flip that becomes live.
    """
    problems = check(_unsafe_wf(tmp_path))
    assert len(problems) == 1, problems
    assert "allow-unsafe-pr-checkout" in problems[0]
    assert "'build'" in problems[0]


def test_unsafe_checkout_detected_unquoted(tmp_path: Path) -> None:
    """`true` parses as a bool, `'true'` as a str; both are the same opt-in."""
    assert len(check(_unsafe_wf(tmp_path, value="true"))) == 1


def test_a_false_value_is_not_an_opt_in(tmp_path: Path) -> None:
    assert check(_unsafe_wf(tmp_path, value="'false'")) == []


def test_unsafe_checkout_respects_a_job_exemption(tmp_path: Path) -> None:
    write_allowlist(
        tmp_path,
        """
        exempt:
          - workflow: ci.yml
            jobs: [build]
        """,
    )
    assert check(_unsafe_wf(tmp_path)) == []


def test_unsafe_checkout_under_workflow_run_is_exempt(tmp_path: Path) -> None:
    """The generated orchestrators' shape: require-ci-approval cannot gate here.

    Under workflow_run it reports `not-a-pull-request` and passes every time, so
    demanding it would be demanding an inert gate.
    """
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
    """Under pull_request_target an ungated unsafe checkout breaks two rules.

    Pins that the new check is additive rather than folded into the existing
    walk -- a regression here would silently drop one of the two messages.
    """
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


# --- the light-install contract ----------------------------------------------

#: Everything pyproject.toml keeps out of the base dependency set. This module is
#: exported as a pre-commit hook, so pre-commit builds a venv from the base set on
#: every developer machine and in every consuming repo; boto3 alone is a ~50MB
#: download and troika is a git clone, to lint YAML.
_OPTIONAL_DEPS = {"boto3", "botocore", "pydantic", "jinja2", "troika"}


def test_the_linter_imports_nothing_optional() -> None:
    """check_ci_approval must stay installable from the base dependency set alone.

    Asserted on the source rather than by importing, so it holds even on a machine
    that happens to have the optional packages available.
    """
    src = Path(check.__globals__["__file__"]).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert not (imported & _OPTIONAL_DEPS), (
        f"check_ci_approval imports {sorted(imported & _OPTIONAL_DEPS)}, which pyproject.toml "
        "keeps in an extra. Either drop the import or the pre-commit hook gets heavy again."
    )
    # It also must not reach them indirectly through a sibling module.
    assert not (imported & {"ci_infrastructure"}), imported
