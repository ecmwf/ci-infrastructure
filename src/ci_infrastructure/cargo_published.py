# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Of the named workspace crates, those the registry has at their version already, one per line.

Reads `cargo metadata --no-deps --format-version 1` on stdin for the versions. A registry never
takes a version twice, so a release leaves these out: crates that move on their own schedule.
The index is crates.io's unless `--index` names another sparse one.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import click

CRATES_IO_INDEX: Final = "https://index.crates.io"
_USER_AGENT: Final = "ci-infrastructure (https://github.com/ecmwf/ci-infrastructure)"


def index_path(name: str) -> str:
    """Where a sparse index keeps a crate's versions, by the registry index format."""
    n = name.lower()
    if len(n) <= 2:
        return f"{len(n)}/{n}"
    if len(n) == 3:
        return f"3/{n[0]}/{n}"
    return f"{n[:2]}/{n[2:4]}/{n}"


def versions_in(index: str, name: str) -> set[str]:
    """The versions the sparse index lists for `name`; none for a crate the registry never saw."""
    url = f"{index.rstrip('/')}/{index_path(name)}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return set()
        raise
    return {json.loads(line)["vers"] for line in body.splitlines() if line.strip()}


def published(metadata: Mapping[str, Any], names: Sequence[str], versions_of: Callable[[str], set[str]]) -> list[str]:
    """The names whose workspace version the registry has; a name outside the workspace is an error."""
    versions = {p["name"]: p["version"] for p in metadata.get("packages", [])}
    if unknown := [n for n in names if n not in versions]:
        raise click.ClickException(f"not a crate of the workspace: {', '.join(unknown)}")
    return [n for n in names if versions[n] in versions_of(n)]


@click.command(help=__doc__)
@click.argument("names", nargs=-1)
@click.option("--index", default=CRATES_IO_INDEX, show_default=True, help="The registry's sparse index URL.")
def main(names: tuple[str, ...], index: str) -> None:
    for name in published(json.load(sys.stdin), names, lambda n: versions_in(index, n)):
        click.echo(name)


if __name__ == "__main__":
    main()
