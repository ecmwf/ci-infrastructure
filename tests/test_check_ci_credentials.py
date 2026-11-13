# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ci_infrastructure.check_ci_credentials import check, main

PR = "on:\n  pull_request:\n"
CHECKOUT = "      - uses: actions/checkout@v6\n"
BUILD = "      - uses: ecmwf/ci-infrastructure/actions/run-job-script@main\n"


def _workflow(tmp_path: Path, text: str, name: str = "ci.yml") -> Path:
    path = tmp_path / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def _job(steps: str, on: str = PR, top: str = "") -> str:
    return f"{on}{top}jobs:\n  build:\n    runs-on: arc-runner-normal\n    steps:\n{steps}"


def _mint(app: str, extra: str = "          repositories: eckit\n") -> str:
    return (
        "      - uses: ecmwf/ci-infrastructure/actions/mint-token@main\n"
        f"        with:\n          app: {app}\n"
        f"          client-id: ${{{{ secrets.CI_{app.upper()}_APP_CLIENT_ID }}}}\n"
        f"          private-key: ${{{{ secrets.CI_{app.upper()}_APP_PRIVATE_KEY }}}}\n{extra}"
    )


def _rules(problems: list[str]) -> list[str]:
    return [next(word for word in p.split() if word in ("R1", "R2", "R3", "R4")) for p in problems]


def test_a_build_leg_may_hold_the_reader_key_and_s3(tmp_path: Path) -> None:
    steps = (
        _mint("reader")
        + CHECKOUT
        + BUILD
        + "        env:\n          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}\n"
    )
    assert check(_workflow(tmp_path, _job(steps))) == []


def test_a_build_leg_must_not_mint_another_app(tmp_path: Path) -> None:
    problems = check(_workflow(tmp_path, _job(_mint("dispatcher") + CHECKOUT)))
    assert _rules(problems) == ["R1"]
    assert "CI_DISPATCHER_APP_PRIVATE_KEY" in problems[0] and "the dispatcher App" in problems[0]


def test_a_job_without_code_may_hold_any_key(tmp_path: Path) -> None:
    assert check(_workflow(tmp_path, _job(_mint("reporter")))) == []


@pytest.mark.parametrize(
    "on",
    [
        "on:\n  schedule:\n    - cron: '0 3 * * *'\n",
        "on:\n  push:\n    branches: [develop, master]\n",
        "on:\n  push:\n    tags: ['*']\n",
    ],
)
def test_reviewed_code_only_is_trusted(tmp_path: Path, on: str) -> None:
    assert check(_workflow(tmp_path, _job(_mint("dispatcher") + CHECKOUT, on=on))) == []


@pytest.mark.parametrize(
    "on",
    [
        "on:\n  push:\n",
        "on:\n  push:\n    branches: ['sync/**']\n",
        "on:\n  workflow_dispatch:\n",
        "on:\n  workflow_call:\n",
    ],
)
def test_other_triggers_may_run_code_under_test(tmp_path: Path, on: str) -> None:
    assert _rules(check(_workflow(tmp_path, _job(_mint("dispatcher") + CHECKOUT, on=on)))) == ["R1"]


def test_pull_request_target_runs_code_only_when_it_checks_the_pull_request_out(tmp_path: Path) -> None:
    on = "on:\n  pull_request_target:\n"
    assert check(_workflow(tmp_path, _job(_mint("reporter") + CHECKOUT, on=on))) == []
    head = CHECKOUT + "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
    assert _rules(check(_workflow(tmp_path, _job(_mint("reporter") + head, on=on)))) == ["R1"]


def test_a_called_workflow_runs_under_its_callers_triggers(tmp_path: Path) -> None:
    callee = _workflow(tmp_path, _job(_mint("dispatcher") + CHECKOUT, on="on:\n  workflow_call:\n"), "build.yml")
    _workflow(tmp_path, "on:\n  pull_request_target:\njobs:\n  call:\n    uses: ./.github/workflows/build.yml\n")
    assert "pull_request_target" in check(callee)[0]


def test_hpc_secrets_only_beside_build_on_hpc(tmp_path: Path) -> None:
    pull = "        env:\n          TOKEN: ${{ secrets.ECCR_PULL_ROBOT_TOKEN }}\n"
    hpc = "      - uses: ecmwf/ci-infrastructure/actions/build-on-hpc@main\n"
    assert check(_workflow(tmp_path, _job(CHECKOUT + hpc + pull))) == []
    assert _rules(check(_workflow(tmp_path, _job(CHECKOUT + BUILD + pull)))) == ["R1"]


def test_a_secret_named_in_plain_text_is_not_held(tmp_path: Path) -> None:
    steps = CHECKOUT + "      - run: curl https://secrets.ecmwf.int\n"
    assert check(_workflow(tmp_path, _job(steps))) == []


def test_reader_mints_name_their_repositories(tmp_path: Path) -> None:
    assert _rules(check(_workflow(tmp_path, _job(_mint("reader", extra=""))))) == ["R2"]


def test_create_github_app_token_is_not_used_directly(tmp_path: Path) -> None:
    steps = "      - uses: actions/create-github-app-token@v3\n"
    assert _rules(check(_workflow(tmp_path, _job(steps, on="on:\n  schedule:\n    - cron: '0 3 * * *'\n")))) == ["R3"]


def test_workflow_level_env_holds_no_privileged_secret(tmp_path: Path) -> None:
    s3 = "env:\n  AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}\n"
    assert check(_workflow(tmp_path, _job(CHECKOUT, top=s3))) == []
    key = "env:\n  TOKEN: ${{ secrets.PYPI_API_TOKEN }}\n"
    assert _rules(check(_workflow(tmp_path, _job(CHECKOUT, top=key)))) == ["R4"]


def test_no_job_gets_every_secret(tmp_path: Path) -> None:
    steps = "      - run: echo '${{ toJSON(secrets) }}'\n"
    assert _rules(check(_workflow(tmp_path, _job(steps)))) == ["R4"]


@pytest.mark.parametrize(
    ("uses", "rules"),
    [
        ("ecmwf/eckit/.github/workflows/cross-repo-trigger.yml@develop", []),
        ("other/repo/.github/workflows/x.yml@v1", ["R4"]),
    ],
)
def test_secrets_are_inherited_only_within_the_owner(tmp_path: Path, uses: str, rules: list[str]) -> None:
    text = f"{PR}jobs:\n  call:\n    uses: {uses}\n    secrets: inherit\n"
    assert _rules(check(_workflow(tmp_path, text))) == rules


def test_an_exemption_needs_a_reason(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path, _job(_mint("dispatcher") + CHECKOUT))
    allowlist = tmp_path / ".github" / "ci-approval-allowlist.yml"
    allowlist.write_text("credentials:\n  - workflow: ci.yml\n    jobs: [build]\n")
    assert "needs a `reason:`" in check(workflow)[0]
    allowlist.write_text("credentials:\n  - workflow: ci.yml\n    jobs: [build]\n    reason: main-only environment\n")
    assert check(workflow) == []


def test_main_reports_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(_workflow(tmp_path, _job(_mint("dispatcher") + CHECKOUT)))]) == 1
    assert "R1" in capsys.readouterr().err
