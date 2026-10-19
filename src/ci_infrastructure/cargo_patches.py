# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Cargo patches that point the crates `.ci/manifest.toml` lists at the commit their repo's C++ was built from.

Reads `cargo metadata --no-deps --format-version 1` on stdin and prints `--config` values, one per
line. With `--check`, reads the metadata cargo resolved with those patches instead and fails for a
patch it did not use. Either fails where Cargo.toml and the manifest disagree.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping
from typing import Any, Final

import click

_GIT_SOURCE_RE: Final = re.compile(
    r"^git\+(?P<url>(?:ssh://git@github\.com/|https://github\.com/)(?P<repo>[^/]+/[^/?#]+?)(?:\.git)?)(?:[?#]|$)"
)
_CRATES_IO: Final = "registry+https://github.com/rust-lang/crates.io-index"


def _declared(metadata: Mapping[str, Any]) -> dict[str, str | None]:
    """The source each dependency of the workspace members is declared from; None for a path."""
    members = set(metadata.get("workspace_members") or ())
    return {
        d["name"]: d.get("source")
        for p in metadata.get("packages", [])
        if not members or p["id"] in members
        for d in p.get("dependencies", [])
    }


def patches(metadata: Mapping[str, Any], crates: Mapping[str, str], shas: Mapping[str, str]) -> Iterator[str]:
    """`crates` maps a crate to its repo, `shas` a resolved dep's repo to its commit."""
    declared = _declared(metadata)
    if missing := sorted(c for c in crates if c not in declared):
        raise click.ClickException(f"the manifest lists crates no Cargo.toml depends on: {', '.join(missing)}")
    unlisted = sorted(
        name
        for name, source in declared.items()
        if name not in crates and (m := _GIT_SOURCE_RE.match(source or "")) and m["repo"] in shas
    )
    if unlisted:
        raise click.ClickException(
            f"Cargo.toml takes {', '.join(unlisted)} over git from a dep's repo; list them in the dep's `crates`"
        )
    for crate, repo in sorted(crates.items()):
        if repo not in shas:
            raise click.ClickException(f"{crate}: {repo} is not a dep of this leg")
        source = declared[crate] or ""
        if source == _CRATES_IO:
            key, target = f"patch.crates-io.{crate}", f"https://github.com/{repo}.git"
        elif m := _GIT_SOURCE_RE.match(source):
            # A patch must name another source than the one it replaces; the job's git config
            # turns either URL into the same authenticated https fetch.
            key = f'patch."{m["url"]}".{crate}'
            target = (
                f"https://github.com/{repo}.git"
                if m["url"].startswith("ssh://")
                else f"ssh://git@github.com/{repo}.git"
            )
        else:
            raise click.ClickException(f"{crate}: Cargo.toml takes it from {source or 'a path'}, not crates.io or git")
        # One dotted key per value: --config takes no inline tables.
        yield f'{key}.git="{target}"'
        yield f'{key}.rev="{shas[repo]}"'


def _from_patch(source: str, repo: str, sha: str) -> bool:
    return bool((m := _GIT_SOURCE_RE.match(source)) and m["repo"] == repo and sha in source)


def unused(metadata: Mapping[str, Any], crates: Mapping[str, str], shas: Mapping[str, str]) -> list[str]:
    """The patched crates cargo resolved from elsewhere: the version at the commit misses Cargo.toml's requirement."""
    reqs = {
        d["name"]: d["req"]
        for p in metadata.get("packages", [])
        if p["id"] in set(metadata.get("workspace_members") or ())
        for d in p.get("dependencies", [])
    }
    sources = {p["name"]: p.get("source") or "" for p in metadata.get("packages", [])}
    return [
        f"{crate}: Cargo.toml requires {reqs.get(crate, '?')}, which {repo}@{shas[repo]} does not provide"
        for crate, repo in sorted(crates.items())
        if not _from_patch(sources.get(crate, ""), repo, shas[repo])
    ]


@click.command(help=__doc__)
@click.option("--crate", "crates", multiple=True, help="crate=owner/repo, as the manifest's `crates` list it.")
@click.option("--dep", "deps", multiple=True, help="owner/repo@sha, as resolve-deps built it.")
@click.option("--check", is_flag=True, help="Read the patched, resolved metadata and fail for an unused patch.")
def main(crates: tuple[str, ...], deps: tuple[str, ...], check: bool) -> None:
    by_crate = dict(c.split("=", 1) for c in crates)
    shas = dict(d.split("@", 1) for d in deps)
    if not (text := sys.stdin.read().strip()):
        raise click.ClickException("no cargo metadata on stdin: cargo failed above")
    metadata = json.loads(text)
    if check:
        if problems := unused(metadata, by_crate, shas):
            raise click.ClickException("cargo did not use the patch for " + "; ".join(problems))
        return
    for line in patches(metadata, by_crate, shas):
        print(line)


if __name__ == "__main__":
    main()
