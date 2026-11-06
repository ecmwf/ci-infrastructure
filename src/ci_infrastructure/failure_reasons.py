# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Why a workflow run failed: the job that actually broke, and the jobs that passed its failure on.

A consumer's resolve or fetch-deps error links the producer run that was red; that run's
own errors often link one further up. Following those links ends at the root cause: a failed
job whose errors link no further. Only the root causes are spelled out; every job on the way
is listed by its link, so the most downstream repo shows what broke without a nested dump.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from typing import Any, Final

from ._github_api import gh_api_rest

MAX_DEPTH: Final = 4
MAX_ROOTS: Final = 3
MAX_VIA: Final = 5
MAX_MESSAGE: Final = 200
_RUN_URL_RE: Final = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
# Restate that a job failed without saying why.
_NOISE_RE: Final = re.compile(
    r"^(Process completed with exit code \d+\.?"
    r"|Executing the custom container implementation failed\..*"
    r"|Error: failed to run script step: .*"
    r"|\d+ dep\(s\) could not be fetched\.)$"
)
_NOISE_TITLES: Final = frozenset({"CI result"})
# Collect results only lists the other jobs.
_NOISE_JOBS: Final = frozenset({"collect results", "collect HPC results"})


@dataclasses.dataclass(frozen=True)
class Root:
    """A failed job whose errors link no further upstream run."""

    repo: str
    job: str
    message: str
    url: str  # The job's, under its run's URL.


@dataclasses.dataclass
class Chain:
    roots: list[Root] = dataclasses.field(default_factory=list)
    via: list[str] = dataclasses.field(default_factory=list)


def run_ref(url: str) -> tuple[str, int] | None:
    """(owner/repo, run id) of the first run URL in `url`."""
    m = _RUN_URL_RE.search(url)
    return (m.group(1), int(m.group(2))) if m else None


def job_url(repo: str, run_id: int, job_id: int) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}"


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


def _shorten(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE else message[: MAX_MESSAGE - 1] + "…"


def _walk(
    repo: str, run_id: int, token: str | None, depth: int, chain: Chain, explained: dict[tuple[str, int], bool]
) -> bool:
    """Add the run's root causes and intermediate jobs to `chain`; whether the run has a failed job."""
    if (repo, run_id) in explained:
        return explained[repo, run_id]
    explained[repo, run_id] = False  # A cycle ends here.
    data = gh_api_rest(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100", token)
    jobs: list[dict[str, Any]] = data.get("jobs") or [] if isinstance(data, dict) else []
    failed = [j for j in jobs if j.get("conclusion") == "failure" and j.get("name") not in _NOISE_JOBS]
    for job in failed:
        url = job_url(repo, run_id, int(job["id"]))
        messages = _failure_messages(repo, int(job["id"]), token)
        upstream = {ref for m in messages if (ref := run_ref(m)) is not None and ref != (repo, run_id)}
        passed_on = depth + 1 < MAX_DEPTH and any(
            [_walk(*ref, token, depth + 1, chain, explained) for ref in sorted(upstream)]
        )
        if passed_on:
            if url not in chain.via:
                chain.via.append(url)
        else:
            reason = next((m for m in messages if run_ref(m) is None), messages[0] if messages else "failed")
            chain.roots.append(Root(repo, str(job.get("name")), _shorten(reason), url))
    explained[repo, run_id] = bool(failed)
    return bool(failed)


def explain_run(repo: str, run_id: int, token: str | None) -> list[str]:
    """The root causes of the run's failure, each with its job link, then the jobs that passed them on."""
    chain = Chain()
    _walk(repo, run_id, token, 0, chain, {})
    # The legs of one matrix usually fail for the same reason; show one of them per run.
    roots: dict[str, list[Root]] = {}
    for root in chain.roots:
        roots.setdefault(root.url.rsplit("/job/", 1)[0], []).append(root)
    lines: list[str] = []
    for group in list(roots.values())[:MAX_ROOTS]:
        first = group[0]
        others = f" (and {len(group) - 1} more failed job(s) of that run)" if len(group) > 1 else ""
        lines.append(f"  caused by {first.repo} › {first.job}: {first.message}{others}")
        lines.append(f"    {first.url}")
    if len(roots) > MAX_ROOTS:
        lines.append(f"  … and {len(roots) - MAX_ROOTS} more root cause(s)")
    if chain.via:
        lines.append("  via " + " ".join(chain.via[:MAX_VIA]))
        if len(chain.via) > MAX_VIA:
            lines.append(f"  … and {len(chain.via) - MAX_VIA} more job(s) in between")
    return lines


@functools.cache
def explain(url: str | None, token: str | None) -> str:
    """The failure chain of the run at `url`, as lines after a newline; "" if there is none to tell."""
    ref = run_ref(url or "")
    lines = explain_run(*ref, token) if ref is not None else []
    return "".join(f"\n{line}" for line in lines)
