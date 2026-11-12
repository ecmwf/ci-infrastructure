# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from ci_infrastructure._errors import CIError
from ci_infrastructure.regenerate_targets import main, targets

CONFIG = Path(__file__).parents[1] / ".github" / "regenerate-repos.toml"
DEFAULTS = {"mode": "pr", "labels": ["x"], "title": "t"}


def test_a_repo_overrides_the_defaults() -> None:
    config = {"defaults": DEFAULTS, "repos": {"a": {}, "b": {"mode": "auto-commit", "labels": []}}}
    assert targets(config) == [
        {"repo": "a", "mode": "pr", "labels": ["x"], "title": "t"},
        {"repo": "b", "mode": "auto-commit", "labels": [], "title": "t"},
    ]
    assert [t["repo"] for t in targets(config, ["b"])] == ["b"]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"defaults": {"mode": "pr"}, "repos": {}}, "missing"),
        ({"defaults": DEFAULTS, "repos": {"a": {"mode": "merge"}}}, "mode must be"),
        ({"defaults": DEFAULTS, "repos": {"a": {"branch": "x"}}}, "unknown keys branch"),
        ({"defaults": DEFAULTS, "repos": {"a": {"labels": "x"}}}, "labels must be a list"),
        ({"defaults": DEFAULTS, "repos": {"a": {"labels": ["x,y"]}}}, "without commas"),
    ],
)
def test_a_bad_config_is_an_error(config: dict[str, object], message: str) -> None:
    with pytest.raises(CIError, match=message):
        targets(config)


def test_an_unknown_repo_is_an_error() -> None:
    with pytest.raises(CIError, match="not in \\[repos\\]: c"):
        targets({"defaults": DEFAULTS, "repos": {"a": {}}}, ["c"])


def test_the_checked_in_config() -> None:
    repos = targets(tomllib.loads(CONFIG.read_text()))
    assert {"ecbuild", "eckit", "anemoi-core"} <= {t["repo"] for t in repos}
    result = CliRunner().invoke(main, ["--config", str(CONFIG), "--repos", "eckit,atlas"])
    assert result.exit_code == 0, result.output
    assert [t["repo"] for t in json.loads(result.output)] == ["eckit", "atlas"]
