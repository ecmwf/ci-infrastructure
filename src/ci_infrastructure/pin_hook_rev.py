# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the ci-infrastructure hooks in a `.pre-commit-config.yaml` to a commit, keeping the file's layout.

The regenerate bot runs it, so its PR is checked by the hooks of the generator that wrote it.
A config without the ci-infrastructure repo is left as it is.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import click

_REV: Final = re.compile(
    r"(^\s*-\s*repo:\s*['\"]?https://github\.com/ecmwf/ci-infrastructure(?:\.git)?['\"]?\s*\n"
    r"(?:(?!\s*-\s*repo:).*\n)*?"
    r"\s*rev:\s*)['\"]?[^\s'\"#]+['\"]?",
    re.MULTILINE,
)


def pin(config: str, sha: str) -> str:
    return _REV.sub(lambda m: m.group(1) + sha, config)


@click.command(help=__doc__)
@click.argument("sha")
@click.option("--config", "path", type=click.Path(path_type=Path), default=Path(".pre-commit-config.yaml"))
def main(sha: str, path: Path) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if (pinned := pin(text, sha)) != text:
        path.write_text(pinned, encoding="utf-8")
        click.echo(f"{path}: ci-infrastructure hooks pinned to {sha}")


if __name__ == "__main__":
    main()
