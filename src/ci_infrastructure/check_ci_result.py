# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Fail a ci.yml without a well-formed `ci-result` job.

The default-branch ruleset requires the single check `ci-result`, a commit status
that actions/ci-result posts: the job runs `if: always()` with `statuses: write`
and feeds the action `toJSON(needs)`. Its own check run is `collect results`, so a
fork's skipped pull_request run never stands in for the status ci-fork.yml posts.
Which jobs it needs is up to the repo; a failure propagates as a skip, which
counts as failed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

JOB = "ci-result"
# The job's own check run must not pass for the status, nor vary per repo.
JOB_NAME = "collect results"
# Remote (consumers) and local (ci-infrastructure itself) spellings.
ACTIONS = ("ecmwf/ci-infrastructure/actions/ci-result@", "./actions/ci-result")
NEEDS_INPUT = "${{ toJSON(needs) }}"
_SQUASHED_INPUT = "".join(NEEDS_INPUT.split())
DOCS = "see docs/configuring/ci-yml.rst in ecmwf/ci-infrastructure"


def _needs(job: dict[str, Any]) -> set[str]:
    raw = job.get("needs", [])
    return {raw} if isinstance(raw, str) else {str(n) for n in raw} if isinstance(raw, list) else set()


def _uses_action(job: dict[str, Any]) -> bool:
    for step in job.get("steps") or []:
        if not isinstance(step, dict) or not str(step.get("uses", "")).startswith(ACTIONS):
            continue
        with_block = step.get("with")
        if isinstance(with_block, dict) and "".join(str(with_block.get("needs", "")).split()) == _SQUASHED_INPUT:
            return True
    return False


def check(workflow: Path) -> list[str]:
    try:
        doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        return [f"{workflow}: cannot read: {e}"]
    raw_jobs = doc.get("jobs") if isinstance(doc, dict) else None
    jobs = {str(k): v if isinstance(v, dict) else {} for k, v in raw_jobs.items()} if isinstance(raw_jobs, dict) else {}

    if JOB not in jobs:
        return [f"{workflow}: no job {JOB!r}, the required check that collects the other jobs; {DOCS}"]
    job = jobs[JOB]
    problems = []
    if str(job.get("if", "")).strip() != "always()":
        problems.append(
            f"{workflow}: job {JOB!r} must run `if: always()`, or a failed job skips it and it reports success"
        )
    if not _uses_action(job):
        problems.append(f"{workflow}: job {JOB!r} must use {ACTIONS[0]}main with `needs: {NEEDS_INPUT}`")

    if job.get("name") != JOB_NAME:
        problems.append(f"{workflow}: job {JOB!r} needs `name: {JOB_NAME}`; the action posts {JOB!r} itself")
    permissions = job.get("permissions", doc.get("permissions"))
    if not (isinstance(permissions, dict) and permissions.get("statuses") == "write"):
        problems.append(f"{workflow}: job {JOB!r} needs `permissions: statuses: write` to post {JOB!r}")
    if not _needs(job):
        problems.append(f"{workflow}: job {JOB!r} needs no job, so it checks nothing")
    return problems


def main(argv: list[str] | None = None) -> int:
    problems: list[str] = []
    for arg in argv if argv is not None else sys.argv[1:]:
        problems.extend(check(Path(arg)))
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
