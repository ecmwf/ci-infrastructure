# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Print the repos regenerate-repos.yml writes to, with their settings, as a JSON list."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import click

from ._errors import CIError

MODES: Final = ("pr", "pr-auto-merge", "auto-commit")
_KEYS: Final = {"mode": str, "labels": list, "title": str}


def _settings(where: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if unknown := sorted(set(raw) - set(_KEYS)):
        raise CIError(f"{where}: unknown keys {', '.join(unknown)}; known are {', '.join(_KEYS)}")
    for key, value in raw.items():
        if not isinstance(value, _KEYS[key]):
            raise CIError(f"{where}: {key} must be a {_KEYS[key].__name__}")
    if "mode" in raw and raw["mode"] not in MODES:
        raise CIError(f"{where}: mode must be one of {', '.join(MODES)}, not {raw['mode']!r}")
    if any(not isinstance(label, str) or "," in label for label in raw.get("labels", ())):
        raise CIError(f"{where}: labels must be strings without commas")
    return dict(raw)


def targets(config: Mapping[str, Any], only: Sequence[str] = ()) -> list[dict[str, Any]]:
    defaults = _settings("[defaults]", config.get("defaults", {}))
    if missing := sorted(set(_KEYS) - set(defaults)):
        raise CIError(f"[defaults]: missing {', '.join(missing)}")
    repos = config.get("repos", {})
    if unknown := sorted(set(only) - set(repos)):
        raise CIError(f"not in [repos]: {', '.join(unknown)}")
    return [
        {"repo": repo, **defaults, **_settings(f"[repos.{repo}]", raw)}
        for repo, raw in repos.items()
        if not only or repo in only
    ]


@click.command(help=__doc__)
@click.option(
    "--config",
    "path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path(".github/regenerate-repos.toml"),
)
@click.option("--repos", default="", help="Space- or comma-separated subset of the configured repos.")
def main(path: Path, repos: str) -> None:
    only = repos.replace(",", " ").split()
    click.echo(json.dumps(targets(tomllib.loads(path.read_text(encoding="utf-8")), only)))


if __name__ == "__main__":
    main()
