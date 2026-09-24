# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Fail a workflow that can run fork code without hanging every job off the gate.

Checked trigger shapes:

  - `pull_request_target` runs with the base repo's secrets and a read/write
    GITHUB_TOKEN, even on GitHub-hosted runners.
  - `pull_request` puts fork code on whatever runner the job names; on ARC and
    self-hosted builders that is our hardware.

Any step setting `allow-unsafe-pr-checkout` must also sit in a gated job.

The gate (`actions/require-ci-approval`) works through `needs:` only: a job
skipped by an `if:` reports Success. So each job must list the gate directly;
a transitive path is not accepted, as one misplaced edge would ungate the rest.

Private and internal repos are skipped. Exemptions live in
.github/ci-approval-allowlist.yml.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ALLOWLIST_NAME = "ci-approval-allowlist.yml"
MANIFEST_PATH = (".ci", "manifest.toml")
# Remote (consumers) and local (ci-infrastructure itself) spellings.
GATE_ACTIONS = (
    "ecmwf/ci-infrastructure/actions/require-ci-approval",
    "./actions/require-ci-approval",
)
OPEN_TO_OUTSIDERS = frozenset({"public", ""})
# Lets actions/checkout fetch a fork's ref into a trusted context.
UNSAFE_CHECKOUT_INPUT = "allow-unsafe-pr-checkout"

# Anything else (ARC, self-hosted arrays, `${{ }}` expressions) counts as ours.
GITHUB_HOSTED = frozenset(
    {
        "ubuntu-latest",
        "ubuntu-slim",
        "ubuntu-24.04",
        "ubuntu-22.04",
        "ubuntu-20.04",
        "ubuntu-24.04-arm",
        "ubuntu-22.04-arm",
        "macos-latest",
        "macos-15",
        "macos-14",
        "macos-13",
        "windows-latest",
        "windows-2025",
        "windows-2022",
        "windows-2019",
    }
)


def _triggers(doc: dict[Any, Any]) -> set[str]:
    """Names of a workflow's `on:` keys. PyYAML reads the bare key `on` as True."""
    raw = doc.get(True, doc.get("on"))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {t for t in raw if isinstance(t, str)}
    if isinstance(raw, dict):
        return {str(k) for k in raw}
    return set()


def _is_github_hosted(runs_on: Any) -> bool:
    return isinstance(runs_on, str) and "${{" not in runs_on and runs_on in GITHUB_HOSTED


def _gate_reason(doc: dict[Any, Any], jobs: dict[str, Any]) -> str | None:
    triggers = _triggers(doc)
    if "pull_request_target" in triggers:
        return "pull_request_target runs with this repository's secrets"
    if "pull_request" in triggers:
        for name, job in jobs.items():
            if "uses" in job:
                continue
            if not _is_github_hosted(job.get("runs-on")):
                return f"pull_request reaches a non-GitHub-hosted runner in job {name!r}"
    return None


def _steps(jobs: dict[str, Any]) -> Iterator[tuple[str, dict[Any, Any]]]:
    for name, job in jobs.items():
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                yield name, step


def _gate_job_names(jobs: dict[str, Any]) -> set[str]:
    return {name for name, step in _steps(jobs) if str(step.get("uses", "")).startswith(GATE_ACTIONS)}


def _unsafe_checkout_jobs(jobs: dict[str, Any]) -> set[str]:
    """Jobs with a step that opts in to checking out a fork's code."""
    found = set()
    for name, step in _steps(jobs):
        with_block = step.get("with")
        if not isinstance(with_block, dict):
            continue
        value = with_block.get(UNSAFE_CHECKOUT_INPUT)
        # `true` and `'true'` are the same opt-in.
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            found.add(name)
    return found


def _needs(job: dict[str, Any]) -> set[str]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {n for n in raw if isinstance(n, str)}
    return set()


def _open_to_outsiders(workflow: Path) -> bool:
    """From [package].visibility in .ci/manifest.toml; no (or broken) manifest counts as public."""
    manifest = workflow.parent.parent.parent.joinpath(*MANIFEST_PATH)
    if not manifest.is_file():
        return True
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return True
    visibility = str(data.get("package", {}).get("visibility", "")).lower()
    return visibility in OPEN_TO_OUTSIDERS


def _load_allowlist(workflow: Path) -> tuple[set[str], set[tuple[str, str]]]:
    """(whole workflows, (workflow, job) pairs) exempted, keyed by file name."""
    path = workflow.parent.parent / ALLOWLIST_NAME
    if not path.is_file():
        return set(), set()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    whole: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for entry in doc.get("exempt") or []:
        if not isinstance(entry, dict) or "workflow" not in entry:
            continue
        wf = str(entry["workflow"])
        jobs = entry.get("jobs")
        if jobs is None:
            whole.add(wf)
        else:
            pairs.update((wf, str(j)) for j in jobs)
    return whole, pairs


def check(workflow: Path) -> list[str]:
    try:
        doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{workflow}: cannot parse: {str(exc).splitlines()[0]}"]
    if not isinstance(doc, dict):
        return []

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return []
    jobs = {str(k): (v if isinstance(v, dict) else {}) for k, v in jobs.items()}

    if not _open_to_outsiders(workflow):
        return []

    whole, pairs = _load_allowlist(workflow)
    if workflow.name in whole:
        return []

    gates = _gate_job_names(jobs)
    problems = _unsafe_checkout_problems(doc, jobs, gates, workflow, pairs)

    reason = _gate_reason(doc, jobs)
    if reason is None:
        return problems

    if not gates:
        problems.append(
            f"{workflow}: {reason}, but no job uses {GATE_ACTIONS[0]}. "
            f"Add a gate job, or exempt this workflow in .github/{ALLOWLIST_NAME}."
        )
        return problems

    for name, job in sorted(jobs.items()):
        if (workflow.name, name) in pairs:
            continue
        if name in gates and _is_github_hosted(job.get("runs-on")):
            continue
        if not (_needs(job) & gates):
            gate = sorted(gates)[0]
            problems.append(f"{workflow}: job {name!r} must list {gate!r} in `needs:` ({reason})")
    return problems


def _unsafe_checkout_problems(
    doc: dict[Any, Any],
    jobs: dict[str, Any],
    gates: set[str],
    workflow: Path,
    pairs: set[tuple[str, str]],
) -> list[str]:
    """Jobs checking out fork code must `needs:` the gate; only under PR triggers, elsewhere the gate is inert."""
    if not (_triggers(doc) & {"pull_request", "pull_request_target"}):
        return []
    problems = []
    for name in sorted(_unsafe_checkout_jobs(jobs)):
        if name in gates or (workflow.name, name) in pairs:
            continue
        if _needs(jobs[name]) & gates:
            continue
        problems.append(
            f"{workflow}: job {name!r} sets {UNSAFE_CHECKOUT_INPUT}, running a fork's code with "
            f"this repository's token, secrets and runners, but does not reach "
            f"{GATE_ACTIONS[0]} via `needs:`. Gate the job, or exempt it in "
            f".github/{ALLOWLIST_NAME}."
        )
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
