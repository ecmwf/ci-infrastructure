#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The final `downstream/<lane>` state of a fan-out, with the quarantined consumers' failures ignored.

The quarantine is `.github/downstream-quarantine.toml` on ci-infrastructure's main, read when the fan-out
reports: a change applies to every upstream at once, without regenerating a workflow.
A quarantined consumer still runs and shows its result; its dependents, skipped after its failure, pass anyway.
"""

from __future__ import annotations

import datetime
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import click

from ._errors import CIError
from ._github_api import write_outputs

FAILED: Final = frozenset({"failure", "cancelled"})
_KEYS: Final = {"reason": str, "since": datetime.date}


@dataclass(frozen=True)
class Quarantined:
    reason: str
    since: datetime.date


def load(config: Mapping[str, Any]) -> dict[str, Quarantined]:
    """`[repos.<name>]` tables, each with a reason and the date it was quarantined."""
    if unknown := sorted(set(config) - {"repos"}):
        raise CIError(f"downstream quarantine: unknown tables {', '.join(unknown)}")
    out: dict[str, Quarantined] = {}
    for repo, raw in config.get("repos", {}).items():
        where = f"[repos.{repo}]"
        if not isinstance(raw, dict):
            raise CIError(f"{where} must be a table")
        if unknown := sorted(set(raw) - set(_KEYS)):
            raise CIError(f"{where}: unknown keys {', '.join(unknown)}; known are {', '.join(_KEYS)}")
        for key, kind in _KEYS.items():
            if not isinstance(raw.get(key), kind):
                raise CIError(f"{where}: {key} is required, as a {kind.__name__}")
        out[repo] = Quarantined(raw["reason"], raw["since"])
    return out


@dataclass(frozen=True)
class Verdict:
    state: str
    ignored: dict[str, str]  # job id -> its result, for the quarantined consumers that failed


def judge(results: Mapping[str, str], jobs: Mapping[str, str | None], quarantine: Mapping[str, Quarantined]) -> Verdict:
    """`failure` if a judged job failed or was cancelled, unless it is a quarantined consumer.

    `jobs` maps each judged job id to its consumer repo (`owner/name`), or None for a job that is no consumer.
    """
    if missing := sorted(set(jobs) - set(results)):
        raise CIError(f"no result for {', '.join(missing)}")
    state = "success"
    ignored: dict[str, str] = {}
    for jid, repo in sorted(jobs.items()):
        if results[jid] not in FAILED:
            continue
        if repo is not None and repo.rpartition("/")[2] in quarantine:
            ignored[jid] = results[jid]
        else:
            state = "failure"
    return Verdict(state, ignored)


def summary(verdict: Verdict, jobs: Mapping[str, str | None], quarantine: Mapping[str, Quarantined]) -> str:
    lines = ["### Quarantined consumers", "", "Their failures do not fail this status.", ""]
    for jid, result in verdict.ignored.items():
        entry = quarantine[str(jobs[jid]).rpartition("/")[2]]
        lines.append(f"- `{jid}` ({result}): {entry.reason} Quarantined since {entry.since}.")
    return "\n".join(lines) + "\n"


@click.command(help=__doc__)
@click.option("--results", required=True, help="toJSON(needs) of the reporting job.")
@click.option("--jobs", "jobs_json", required=True, help="JSON object: judged job id -> consumer repo, or null.")
@click.option(
    "--quarantine",
    "path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="The quarantine file; a missing one quarantines nothing.",
)
def main(results: str, jobs_json: str, path: Path) -> None:
    needs = json.loads(results)
    jobs: dict[str, str | None] = json.loads(jobs_json)
    quarantine = load(tomllib.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
    verdict = judge({jid: str(n.get("result", "")) for jid, n in needs.items()}, jobs, quarantine)
    for jid in verdict.ignored:
        click.echo(f"::notice title=Downstream quarantine::{jid} {verdict.ignored[jid]}, ignored: quarantined")
    if verdict.ignored and (step_summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary(verdict, jobs, quarantine))
    write_outputs({"state": verdict.state, "ignored": " ".join(verdict.ignored)})


if __name__ == "__main__":
    main()
