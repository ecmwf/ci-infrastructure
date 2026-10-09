# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Cargo patches that point git crates at the branch resolve-deps built their repo's C++ from.

Reads `cargo metadata --format-version 1` on stdin and prints `--config` values, one per line.
A crate is patched only when its repo resolved to a sync-branch/, a feature/ branch or a
pinned commit; otherwise the branch or rev in Cargo.toml stays.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping
from typing import Any, Final

import click

from .sync_branch import is_sync_branch

_GIT_SOURCE_RE: Final = re.compile(
    r"^git\+(?P<url>(?:ssh://git@github\.com/|https://github\.com/)(?P<repo>[^/]+/[^/?#]+?)(?:\.git)?)"
    r"\?(?P<kind>branch|rev|tag)=(?P<ref>[^#&]+)"
)
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")


def _followed(ref: str) -> str | None:
    """The patch key for a ref resolve-deps chose on purpose; None for a normal one."""
    if _SHA_RE.match(ref):
        return "rev"
    return "branch" if is_sync_branch(ref) else None


def patches(metadata: Mapping[str, Any], refs: Mapping[str, str]) -> Iterator[str]:
    seen: set[tuple[str, str]] = set()
    for package in metadata.get("packages", []):
        m = _GIT_SOURCE_RE.match(package.get("source") or "")
        if m is None or (ref := refs.get(m["repo"])) is None or ref == m["ref"]:
            continue
        if (key := _followed(ref)) is None or (m["url"], package["name"]) in seen:
            continue
        seen.add((m["url"], package["name"]))
        # A patch must name another source than the one it replaces; the job's git config
        # turns either URL into the same authenticated https fetch.
        target = (
            f"https://github.com/{m['repo']}.git"
            if m["url"].startswith("ssh://")
            else f"ssh://git@github.com/{m['repo']}.git"
        )
        # One dotted key per value: --config takes no inline tables.
        crate = f'patch."{m["url"]}".{package["name"]}'
        yield f'{crate}.git="{target}"'
        yield f'{crate}.{key}="{ref}"'


@click.command(help=__doc__)
@click.option("--ref", "refs", multiple=True, help="owner/repo=ref, as resolve-deps chose it.")
def main(refs: tuple[str, ...]) -> None:
    by_repo = dict(r.split("=", 1) for r in refs)
    for line in patches(json.load(sys.stdin), by_repo):
        print(line)


if __name__ == "__main__":
    main()
