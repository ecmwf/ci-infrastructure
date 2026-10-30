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
A job of only payload-only actions (`PAYLOAD_ONLY_ACTIONS`) runs no pull request code and needs no gate.

A workflow called from another (`uses: ./.github/workflows/<it>`) runs under its
caller's events, so it is checked against its callers' triggers too, transitively:
one called from a `pull_request_target` workflow is held to that rule. A job
calling such a workflow is gated when the called workflow passes that check.

With `fork-pull-requests` in the manifest, ci-fork.yml calls ci.yml under
`pull_request_target`, and ci.yml must be fit for it; see `_fork_ci_problems`.

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
# Read the event payload and nothing of the pull request's code: a job of only these needs no gate.
PAYLOAD_ONLY_ACTIONS = (
    "ecmwf/ci-infrastructure/actions/require-label-decision@",
    "./actions/require-label-decision",
    "ecmwf/ci-infrastructure/actions/post-downstream-decision@",
    "./actions/post-downstream-decision",
    "ecmwf/ci-infrastructure/actions/dispatch-downstream@",
    "./actions/dispatch-downstream",
    "ecmwf/ci-infrastructure/actions/sync-branch-prs@",
    "./actions/sync-branch-prs",
    "ecmwf/ci-infrastructure/actions/hpc-label-decision@",
    "./actions/hpc-label-decision",
)

CI_WORKFLOW = "ci.yml"
FORK_CI_WORKFLOW = "ci-fork.yml"
GATE_JOB = "ci-approval"
CHECKOUT_UNDER_TEST = "ecmwf/ci-infrastructure/actions/checkout-under-test"
# Exactly what ci-yml.rst shows: true only for ci-fork.yml's call.
FORK_CHECKOUT_OPT_IN = "${{ github.event_name == 'pull_request_target' }}"
SAME_REPO_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"
FORK_DOCS = "see docs/configuring/fork-pull-requests.rst in ecmwf/ci-infrastructure"

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


def _load(path: Path) -> dict[Any, Any] | None:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


def _jobs_of(doc: dict[Any, Any] | None) -> dict[str, Any]:
    jobs = (doc or {}).get("jobs")
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in jobs.items()} if isinstance(jobs, dict) else {}


def _inherited_triggers(workflow: Path) -> dict[str, str]:
    """Trigger -> the calling workflow it comes from, over every local caller, transitively."""
    siblings = sorted({*workflow.parent.glob("*.yml"), *workflow.parent.glob("*.yaml")})
    calls = {p: {c for job in _jobs_of(_load(p)).values() if (c := _local_workflow(p, job))} for p in siblings}
    out: dict[str, str] = {}
    seen, todo = {workflow}, [workflow]
    while todo:
        callee = todo.pop()
        for caller, callees in calls.items():
            if callee in callees and caller not in seen:
                seen.add(caller)
                todo.append(caller)
                for trigger in _triggers(_load(caller) or {}):
                    out.setdefault(trigger, caller.name)
    return out


def _is_github_hosted(runs_on: Any) -> bool:
    return isinstance(runs_on, str) and "${{" not in runs_on and runs_on in GITHUB_HOSTED


def _gate_reason(doc: dict[Any, Any], jobs: dict[str, Any], inherited: dict[str, str]) -> str | None:
    def via(trigger: str) -> str:
        return f" (through {inherited[trigger]})" if trigger in inherited else ""

    triggers = _triggers(doc) | set(inherited)
    if "pull_request_target" in triggers:
        return f"pull_request_target{via('pull_request_target')} runs with this repository's secrets"
    if "pull_request" in triggers:
        for name, job in jobs.items():
            if "uses" in job:
                continue
            if not _is_github_hosted(job.get("runs-on")):
                return f"pull_request{via('pull_request')} reaches a non-GitHub-hosted runner in job {name!r}"
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
        # `true` and `'true'` are the same opt-in; an expression may be either.
        if value is True or (isinstance(value, str) and (value.strip().lower() == "true" or "${{" in value)):
            found.add(name)
    return found


def _payload_only_jobs(jobs: dict[str, Any]) -> set[str]:
    out = set()
    for name, job in jobs.items():
        steps = job.get("steps") or []
        if steps and all(
            isinstance(st, dict) and str(st.get("uses", "")).startswith(PAYLOAD_ONLY_ACTIONS) for st in steps
        ):
            out.add(name)
    return out


def _needs(job: dict[str, Any]) -> set[str]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {n for n in raw if isinstance(n, str)}
    return set()


def _package(workflow: Path) -> dict[str, Any] | None:
    """[package] of .ci/manifest.toml; None without a (parseable) manifest."""
    manifest = workflow.parent.parent.parent.joinpath(*MANIFEST_PATH)
    if not manifest.is_file():
        return None
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return None
    package = data.get("package", {})
    return package if isinstance(package, dict) else {}


def _open_to_outsiders(workflow: Path) -> bool:
    """From [package].visibility in .ci/manifest.toml; no (or broken) manifest counts as public."""
    package = _package(workflow)
    return package is None or str(package.get("visibility", "")).lower() in OPEN_TO_OUTSIDERS


def _takes_fork_pull_requests(workflow: Path) -> bool:
    return (_package(workflow) or {}).get("fork-pull-requests") is True


def _local_workflow(workflow: Path, job: dict[str, Any]) -> Path | None:
    uses = str(job.get("uses", ""))
    return (
        workflow.parent / uses.removeprefix("./.github/workflows/") if uses.startswith("./.github/workflows/") else None
    )


_CHECKING: set[Path] = set()  # breaks a cycle of local calls


def _delegated_jobs(workflow: Path, jobs: dict[str, Any]) -> set[str]:
    """Jobs calling a local workflow that passes the check under this one's triggers, and has a gate."""
    out = set()
    for name, job in jobs.items():
        called = _local_workflow(workflow, job)
        if called is None or called in _CHECKING or not called.is_file():
            continue
        if _gate_job_names(_jobs_of(_load(called))) and not check(called):
            out.add(name)
    return out


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
    _CHECKING.add(workflow)
    try:
        return _check(workflow)
    finally:
        _CHECKING.discard(workflow)


def _check(workflow: Path) -> list[str]:
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
    inherited = _inherited_triggers(workflow)
    problems = _unsafe_checkout_problems(doc, jobs, gates, workflow, pairs, inherited)
    problems += _fork_ci_problems(doc, jobs, workflow)

    reason = _gate_reason(doc, jobs, inherited)
    if reason is None:
        return problems

    pairs |= {(workflow.name, name) for name in _delegated_jobs(workflow, jobs) | _payload_only_jobs(jobs)}
    if not gates and all((workflow.name, name) in pairs for name in jobs):
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
    inherited: dict[str, str],
) -> list[str]:
    """Jobs checking out fork code must `needs:` the gate; only under PR triggers, elsewhere the gate is inert."""
    if not ((_triggers(doc) | set(inherited)) & {"pull_request", "pull_request_target"}):
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


def _fork_ci_problems(doc: dict[Any, Any], jobs: dict[str, Any], workflow: Path) -> list[str]:
    """ci-fork.yml only with the opt-in; with it, ci.yml as ci-fork.yml calls it."""
    if workflow.name == FORK_CI_WORKFLOW and not _takes_fork_pull_requests(workflow):
        return [f"{workflow}: exists, but .ci/manifest.toml does not set fork-pull-requests = true; {FORK_DOCS}"]
    if workflow.name != CI_WORKFLOW or not _takes_fork_pull_requests(workflow):
        return []

    where = f"{workflow}: fork-pull-requests is set, so ci-fork.yml calls this workflow"
    problems = []
    if "workflow_call" not in _triggers(doc):
        problems.append(f"{where}, but `on:` has no `workflow_call`; {FORK_DOCS}")
    gate = jobs.get(GATE_JOB, {})
    if GATE_JOB not in _gate_job_names(jobs):
        problems.append(f"{where}: its approval gate must be the job {GATE_JOB!r}, which downstream CI looks for")
    elif SAME_REPO_GUARD not in str(gate.get("if", "")):
        problems.append(
            f"{where}: job {GATE_JOB!r} must skip a fork's pull_request run with "
            f"`if: github.event_name != 'pull_request' || {SAME_REPO_GUARD}`; ci-fork.yml runs it instead"
        )
    for name, step in _steps(jobs):
        uses = str(step.get("uses", ""))
        raw_with = step.get("with")
        with_block: dict[Any, Any] = raw_with if isinstance(raw_with, dict) else {}
        if uses.startswith("actions/checkout@") and not with_block.get("repository"):
            problems.append(
                f"{where}: job {name!r} checks this repository out with actions/checkout, which under "
                f"pull_request_target is the base branch, not the pull request; use {CHECKOUT_UNDER_TEST}"
            )
        elif uses.startswith(CHECKOUT_UNDER_TEST) and with_block.get(UNSAFE_CHECKOUT_INPUT) != FORK_CHECKOUT_OPT_IN:
            problems.append(f'{where}: job {name!r} must pass {UNSAFE_CHECKOUT_INPUT}: "{FORK_CHECKOUT_OPT_IN}"')
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
