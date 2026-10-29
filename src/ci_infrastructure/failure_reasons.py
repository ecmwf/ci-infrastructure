# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Why a workflow run failed: its failed jobs' error annotations, following the runs they link to.

A consumer's resolve or fetch-deps error links the producer run that was red; that run's
own errors often link one further up, so the chain ends at the actual failure.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Final

from ._github_api import gh_api_rest

MAX_DEPTH: Final = 4
MAX_JOBS: Final = 5
_RUN_URL_RE: Final = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
# Restate a job's failure without saying why; collect results only lists the other jobs.
_NOISE_RE: Final = re.compile(r"^Process completed with exit code \d+\.?$")
_NOISE_TITLES: Final = frozenset({"CI result"})
_NOISE_JOBS: Final = frozenset({"collect results", "collect HPC results"})


def run_ref(url: str) -> tuple[str, int] | None:
    """(owner/repo, run id) of the first run URL in `url`."""
    m = _RUN_URL_RE.search(url)
    return (m.group(1), int(m.group(2))) if m else None


def _failure_messages(repo: str, job_id: int, token: str | None) -> list[str]:
    data = gh_api_rest(f"repos/{repo}/check-runs/{job_id}/annotations?per_page=50", token)
    messages: list[str] = []
    for a in data if isinstance(data, list) else []:
        if not isinstance(a, dict) or a.get("annotation_level") != "failure" or a.get("title") in _NOISE_TITLES:
            continue
        first = str(a.get("message") or "").strip().splitlines()
        if first and not _NOISE_RE.match(first[0]) and first[0] not in messages:
            messages.append(first[0])
    return messages


def explain_run(
    repo: str, run_id: int, token: str | None, depth: int = 0, seen: set[tuple[str, int]] | None = None
) -> list[str]:
    """One indented line per failed job and error, recursing into the runs those errors link."""
    seen = set() if seen is None else seen
    if (repo, run_id) in seen or depth >= MAX_DEPTH:
        return []
    seen.add((repo, run_id))
    data = gh_api_rest(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
    jobs: list[dict[str, Any]] = data.get("jobs") or [] if isinstance(data, dict) else []
    failed = [j for j in jobs if j.get("conclusion") == "failure" and j.get("name") not in _NOISE_JOBS]
    indent = "  " * (depth + 1)
    lines: list[str] = []
    for job in failed[:MAX_JOBS]:
        messages = _failure_messages(repo, int(job["id"]), token)
        if not messages:
            lines.append(f"{indent}{repo} › {job.get('name')}: failed")
        for message in messages:
            lines.append(f"{indent}{repo} › {job.get('name')}: {message}")
            linked = run_ref(message)
            if linked is not None:
                lines += explain_run(*linked, token, depth + 1, seen)
    if len(failed) > MAX_JOBS:
        lines.append(f"{indent}… and {len(failed) - MAX_JOBS} more failed job(s) of {repo}")
    return lines


@functools.cache
def explain(url: str | None, token: str | None) -> str:
    """The failure chain of the run at `url`, as lines after a newline; "" if there is none to tell."""
    ref = run_ref(url or "")
    lines = explain_run(*ref, token) if ref is not None else []
    return "".join(f"\n{line}" for line in lines)
