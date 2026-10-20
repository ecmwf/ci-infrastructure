# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ci_infrastructure.pin_hook_rev import main, pin

CONFIG = """\
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace

  - repo: https://github.com/ecmwf/ci-infrastructure
    rev: 87422f40d88f590710ce07330002241cf4fc7a2e  # pinned by hand
    hooks:
      - id: check-ci-approval
"""


def test_only_the_ci_infrastructure_rev_moves() -> None:
    assert pin(CONFIG, "a" * 40) == CONFIG.replace("87422f40d88f590710ce07330002241cf4fc7a2e", "a" * 40)


def test_a_rev_after_the_hooks_and_a_quoted_one_move_too() -> None:
    config = "repos:\n- repo: 'https://github.com/ecmwf/ci-infrastructure.git'\n  hooks: []\n  rev: \"v1\"\n"
    assert (
        pin(config, "b" * 40)
        == f"repos:\n- repo: 'https://github.com/ecmwf/ci-infrastructure.git'\n  hooks: []\n  rev: {'b' * 40}\n"
    )


def test_a_config_without_the_repo_or_no_config_is_left_alone(tmp_path: Path) -> None:
    other = CONFIG.split("\n\n")[0] + "\n"
    assert pin(other, "a" * 40) == other
    config = tmp_path / ".pre-commit-config.yaml"
    assert CliRunner().invoke(main, ["a" * 40, "--config", str(config)]).exit_code == 0
    assert not config.exists()
    config.write_text(CONFIG)
    result = CliRunner().invoke(main, ["c" * 40, "--config", str(config)])
    assert result.exit_code == 0 and f"rev: {'c' * 40}  # pinned by hand" in config.read_text()
