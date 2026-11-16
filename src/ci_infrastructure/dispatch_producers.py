#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Start the rebuilds resolve_deps planned (its dispatch-plans output), with the dispatcher App.

The plans come from a job that saw the code under test, so each field is checked again here:
a producer of this owner among `--repositories`, its lane's cross-repo-trigger workflow, or a re-run
of that commit's own CI run. A tampered plan can at most rebuild a known producer.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, cast, get_args

import click

from ._errors import CIError
from ._github_api import (
    Execution,
    _gh,
    _is_build_workflow,
    gh_api_rest,
    lane_suffix,
    probe_workflow_runs,
    select_token,
    write_outputs,
)
from .resolve_deps import DispatchPlan, Ref, Repo, ResolveError, Sha, parse_pin

_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.-]+")
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
# A branch, tag or commit; never an option, never a path out of refs/.
_REF_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


@dataclass(frozen=True)
class Planned:
    """One checked plan, with what its dispatch passes on."""

    plan: DispatchPlan
    from_sha: str
    branch: str
    pin: str


def _ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _REF_RE.fullmatch(value) or ".." in value:
        raise CIError(f"dispatch plan: {field} {value!r} is not a ref")
    return value


def parse_plans(raw: str, owner: str, allowed: Sequence[str]) -> list[Planned]:
    """The plans, each checked; one per producer and lane, or per re-run."""
    try:
        records = json.loads(raw or "[]")
    except json.JSONDecodeError as e:
        raise CIError(f"dispatch plans are not JSON: {e}") from e
    if not isinstance(records, list):
        raise CIError("dispatch plans are not a list")
    names = {r.removeprefix(f"{owner}/") for r in allowed}
    out: dict[tuple[object, ...], Planned] = {}
    for rec in records:
        if not isinstance(rec, dict):
            raise CIError(f"dispatch plan {rec!r} is not an object")
        repo = rec.get("repo")
        if not isinstance(repo, str) or not repo.startswith(f"{owner}/") or repo.split("/", 1)[1] not in names:
            raise CIError(f"dispatch plan: {repo!r} is not one of {', '.join(sorted(names))} of {owner}")
        lane = rec.get("lane")
        if lane not in get_args(Execution):
            raise CIError(f"dispatch plan: lane {lane!r} is not one of {', '.join(get_args(Execution))}")
        lane = cast(Execution, lane)
        sha, from_sha = rec.get("sha"), rec.get("from-sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise CIError(f"dispatch plan: sha {sha!r} is not a commit")
        if not isinstance(from_sha, str) or not _SHA_RE.fullmatch(from_sha):
            raise CIError(f"dispatch plan: from-sha {from_sha!r} is not a commit")
        rerun_id = rec.get("rerun-id")
        if rerun_id is not None and (not isinstance(rerun_id, int) or isinstance(rerun_id, bool) or rerun_id <= 0):
            raise CIError(f"dispatch plan: rerun-id {rerun_id!r} is not a run")
        pin = rec.get("pin", "")
        if not isinstance(pin, str):
            raise CIError(f"dispatch plan: pin {pin!r} is not a string")
        try:
            parse_pin(pin)
        except ResolveError as e:
            raise CIError(f"dispatch plan: {e}") from e
        plan = DispatchPlan(Repo(repo), Ref(_ref(rec.get("ref"), "ref")), Sha(sha), lane, rerun_id=rerun_id)
        key = ("rerun", repo, rerun_id) if rerun_id is not None else (repo, plan.ref, lane)
        out.setdefault(key, Planned(plan, from_sha, _ref(rec.get("branch"), "branch"), pin))
    return list(out.values())


def dispatch_producer_workflow(
    *,
    plan: DispatchPlan,
    dispatcher_repo: str,
    dispatcher_sha: str,
    branch: str,
    fallback_ref: str,
    pin: str,
    token: str | None,
) -> None:
    """Dispatch the producer's lane workflow with rebuild-request, then wait ≤60s for its run to appear.

    Without the wait, fetch_deps could see no run yet and bail instead of polling. `pin` is passed on:
    a producer between the pinned repo and this consumer must build against the pinned commit, or it
    publishes under another deps hash than the one this run waits for.
    """
    dispatch_id = (
        f"resolve-{os.environ.get('GITHUB_RUN_ID', '0')}-"
        f"{os.environ.get('GITHUB_RUN_ATTEMPT', '0')}-{secrets.token_hex(4)}"
    )
    workflow_file = f"cross-repo-trigger{lane_suffix(plan.lane)}.yml"
    cmd = [
        "gh",
        "workflow",
        "run",
        workflow_file,
        "--repo",
        plan.repo,
        "--ref",
        plan.ref,
        "-f",
        f"dispatch-id={dispatch_id}",
        "-f",
        f"from-repo={dispatcher_repo}",
        "-f",
        f"from-sha={dispatcher_sha}",
        "-f",
        "rebuild-request=true",
        "-f",
        f"branch={branch}",
        "-f",
        f"fallback-ref={fallback_ref}",
    ]
    rc, _, stderr = _gh([*cmd, "-f", f"pin={pin}"] if pin else cmd, token)
    if rc != 0 and pin and "Unexpected inputs" in stderr:
        print(
            f"::warning::{workflow_file} in {plan.repo} predates the `pin` input; rebuilding without "
            f"{pin}, so it may publish under another deps hash. Regenerate its downstream-CI workflows."
        )
        rc, _, stderr = _gh(cmd, token)
    if rc != 0:
        raise CIError(
            f"Failed to dispatch {workflow_file} in {plan.repo}@{plan.ref} "
            f"(dispatcher {dispatcher_repo}@{dispatcher_sha[:8]}): {stderr.strip()}"
        )

    print(
        f"::notice::Triggering REBUILD of upstream {plan.repo}@{plan.ref} (sha={plan.sha[:8]}, "
        f"lane={plan.lane}): its artifact was missing, so it will recompile. Downstream build jobs "
        "will WAIT for it to finish."
    )
    print(
        f"  dispatched {workflow_file} in {plan.repo}@{plan.ref} (sha={plan.sha[:8]}, "
        f"dispatch-id={dispatch_id}); "
        "waiting for run to appear"
    )
    _await_run(plan, token)


def rerun_producer_ci(*, plan: DispatchPlan, token: str | None) -> None:
    """Re-run the commit's own CI run, then wait ≤60s for it to show as in flight."""
    run = gh_api_rest(f"repos/{plan.repo}/actions/runs/{plan.rerun_id}", token)
    if not isinstance(run, dict) or run.get("head_sha") != plan.sha or not _is_build_workflow(run, plan.lane):
        raise CIError(f"run {plan.rerun_id} of {plan.repo} is not the CI of {plan.sha[:8]}; not re-running it")
    rc, _, stderr = _gh(["gh", "api", "-X", "POST", f"repos/{plan.repo}/actions/runs/{plan.rerun_id}/rerun"], token)
    if rc != 0 and not probe_workflow_runs(plan.repo, plan.sha, token, lane=plan.lane).in_flight:
        raise CIError(f"Failed to re-run CI run {plan.rerun_id} of {plan.repo}@{plan.sha[:8]}: {stderr.strip()}")
    print(
        f"::notice::Triggering REBUILD of upstream {plan.repo}@{plan.sha[:8]} by re-running its CI "
        f"(run {plan.rerun_id}): its artifact was missing. Downstream build jobs will WAIT for it to finish."
    )
    _await_run(plan, token)


def _await_run(plan: DispatchPlan, token: str | None) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if probe_workflow_runs(plan.repo, plan.sha, token, lane=plan.lane).in_flight:
            print(f"  {plan.repo}@{plan.sha[:8]} run is now visible")
            return
        time.sleep(2)
    print(
        f"::warning::Dispatched run for {plan.repo}@{plan.sha[:8]} did not appear within 60s. "
        "fetch_deps will fail to find it if visibility doesn't catch up."
    )


@click.command(help="Check, or start, the rebuilds in $DISPATCH_PLANS.")
@click.option(
    "--repositories",
    required=True,
    help="Comma- or space-separated repositories a plan may name: the consumer's upstream.",
)
@click.option("--check", is_flag=True, help="Only check the plans, and output the repositories to mint for.")
def main(repositories: str, check: bool) -> None:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    dispatcher_repo = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        planned = parse_plans(os.environ.get("DISPATCH_PLANS", ""), owner, repositories.replace(",", " ").split())
        if check:
            repos = sorted({p.plan.repo.split("/", 1)[1] for p in planned})
            write_outputs({"repositories": ",".join(repos)})
            return
        token = select_token()
        for p in planned:
            if p.plan.rerun_id is not None:
                rerun_producer_ci(plan=p.plan, token=token)
            else:
                dispatch_producer_workflow(
                    plan=p.plan,
                    dispatcher_repo=dispatcher_repo,
                    dispatcher_sha=p.from_sha,
                    branch=p.branch,
                    fallback_ref=str(p.plan.ref),
                    pin=p.pin,
                    token=token,
                )
    except CIError as e:
        raise click.ClickException(str(e)) from e


if __name__ == "__main__":
    main()
