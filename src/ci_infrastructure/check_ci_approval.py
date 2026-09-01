# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Fail a workflow that can run fork code without hanging every job off the gate.

Two trigger shapes hand an outside contributor's pull request something worth
stealing, and both are checked:

  - `pull_request_target` runs in the BASE repository's context, so `secrets.*`
    are populated and GITHUB_TOKEN is read/write. That is true on GitHub-hosted
    runners too -- the credentials are the prize, and unlike an ephemeral VM they
    are reusable.
  - `pull_request` withholds secrets from forks, but still puts the contributor's
    code on whatever runner the job names. On this org's ARC and self-hosted
    builders that is our hardware.

The gate is `actions/require-ci-approval`, and its contract is `needs:` and
nothing else: a job skipped by an `if:` reports Success, so gating that way makes
a required check green for a pull request that never ran. Hence this checks
`needs:` membership directly, per job, rather than accepting a transitive path --
one edge added later in the wrong place silently ungates the rest.

A private or internal repo is skipped: forking it already needs access, so the
author is not an outsider. Exemptions otherwise live in
.github/ci-approval-allowlist.yml next to the workflows.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml

ALLOWLIST_NAME = "ci-approval-allowlist.yml"
MANIFEST_PATH = (".ci", "manifest.toml")
# Both spellings: the pinned remote one consumers use, and the local path
# ci-infrastructure itself can use.
GATE_ACTIONS = (
    "ecmwf/ci-infrastructure/actions/require-ci-approval",
    "./actions/require-ci-approval",
)
OPEN_TO_OUTSIDERS = frozenset({"public", ""})

# Runner labels GitHub itself provides, including `ubuntu-slim` -- despite the
# name that is a GitHub-hosted larger runner, not an ARC one. Anything else -- an
# ARC scale set, a self-hosted label array, or a `${{ }}` expression whose value
# we cannot know here -- counts as ours, and so needs the gate.
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
    """Why this workflow needs the gate, or None if it does not."""
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


def _gate_job_names(jobs: dict[str, Any]) -> set[str]:
    found = set()
    for name, job in jobs.items():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and str(step.get("uses", "")).startswith(GATE_ACTIONS):
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
    """False when forking this repo already requires access, so no fork is untrusted.

    Read from [package].visibility in .ci/manifest.toml. A repo without a manifest
    is treated as public: strict is the safe default to get wrong.
    """
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

    reason = _gate_reason(doc, jobs)
    if reason is None:
        return []

    whole, pairs = _load_allowlist(workflow)
    if workflow.name in whole:
        return []

    gates = _gate_job_names(jobs)
    if not gates:
        return [
            f"{workflow}: {reason}, but no job uses {GATE_ACTIONS[0]}. "
            f"Add a gate job, or exempt this workflow in .github/{ALLOWLIST_NAME}."
        ]

    problems = []
    for name, job in sorted(jobs.items()):
        if name in gates or (workflow.name, name) in pairs:
            continue
        if not (_needs(job) & gates):
            gate = sorted(gates)[0]
            problems.append(f"{workflow}: job {name!r} must list {gate!r} in `needs:` ({reason})")
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
