# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The release gate: decide whether this run may release, and what.

Checks, in order; the first that fails refuses the release:

1. The kill switch (``CD_KILL_SWITCH=on``).
2. The event: a release tag push, or a same-repo pull request labelled ``approved-for-cd`` (rehearsal).
3. ``release`` mode needs allow-production.
4. The tag names the project's VERSION.
5. CI passed on exactly this commit: a ``ci.yml`` run of this repo, ``push`` or ``workflow_dispatch``
   on the commit; for a rehearsal also the ``pull_request`` run of the PR's head. Waits while CI
   runs, or has not started yet, up to ci-wait-seconds; refuses on a failed run.
6. The tag did not move: earlier runs of this workflow for the tag, in any letter case, built the same commit.
7. Each source dep's release-ref is a tag or a commit SHA, never a branch; resolved to a SHA.
8. The workflow file at this commit passes check-cd-workflow.
9. The release legs, each with its ``_release`` context.
10. In a publishing mode, each rpm leg already on Nexus is marked ``already-published``: its build
    is skipped, and publish only records it. Never dropped. The probe authenticates with the Nexus
    credential from ``$NEXUS_AUTH`` (the publish credential: the test repositories are private and
    answer an anonymous probe 403); a refused authenticated probe fails the gate. Without it the
    probe is anonymous, warned about, and a private repository's legs are always rebuilt.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import click

from .. import check_cd_workflow
from .._github_api import IN_PROGRESS_STATUSES, JSON, gh_api_rest, write_outputs
from . import ReleaseError, legs, nexus
from .detect import ReleaseEvent, classify
from .version import MODE_REHEARSAL, MODE_RELEASE, check_tag_matches_version, read_project_version

CI_WORKFLOW: Final = "ci.yml"
KILL_SWITCH_ON: Final = "on"
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_TAG_EVENTS: Final = frozenset({"push", "workflow_dispatch"})

Api = Callable[[str], JSON | None]
Probe = Callable[[str, str], str]


@dataclass(frozen=True)
class GateInputs:
    repo_root: Path
    manifest: Path
    kinds: Sequence[str]
    repository: str
    event_name: str
    ref: str
    sha: str
    payload: Mapping[str, Any]
    workflow_ref: str = ""
    run_id: str = ""
    server_url: str = "https://github.com"
    action_ref: str = ""
    allow_production: bool = False
    nexus_read_urls: Mapping[str, str] = field(default_factory=dict)
    ci_wait_seconds: int = 7200
    poll_seconds: int = 60
    kill_switch: str = ""
    kill_switch_reason: str = ""
    allow_refs: Sequence[str] = ()
    #: ``user:token`` for the Nexus probe; never in an output or the provenance.
    nexus_auth: str = field(default="", repr=False)


@dataclass
class GateResult:
    event: ReleaseEvent
    legs: dict[str, list[dict[str, Any]]]
    source_deps: list[dict[str, str]]
    provenance: dict[str, Any]

    @property
    def publish(self) -> bool:
        return self.event.mode != MODE_REHEARSAL

    def outputs(self) -> dict[str, str]:
        return {
            "mode": self.event.mode,
            "publish": "true" if self.publish else "false",
            "sha": self.event.sha,
            "head-sha": self.event.head_sha,
            "tag": self.event.version.tag or "",
            "version": self.event.version.base,
            "json": json.dumps({k: {"include": v} for k, v in self.legs.items()}, separators=(",", ":")),
            "source-deps": json.dumps(self.source_deps, separators=(",", ":")),
            "provenance": json.dumps(self.provenance, separators=(",", ":")),
        }


def _workflow_path(workflow_ref: str, repository: str) -> str:
    """``o/r/.github/workflows/cd-v3.yml@refs/tags/x`` -> ``.github/workflows/cd-v3.yml``."""
    path = workflow_ref.split("@", 1)[0].removeprefix(f"{repository}/")
    if not path.startswith(".github/workflows/") or ".." in path.split("/"):
        raise ReleaseError(f"cannot tell the workflow file from GITHUB_WORKFLOW_REF {workflow_ref!r}")
    return path


def _runs(api: Api, path: str) -> list[dict[str, Any]]:
    data = api(path)
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    return [r for r in runs or [] if isinstance(r, dict)]


def _accepted(run: Mapping[str, Any], event: ReleaseEvent, repository: str) -> bool:
    if (run.get("head_repository") or {}).get("full_name") != repository:
        return False
    head_sha, kind = run.get("head_sha"), run.get("event")
    if kind in _TAG_EVENTS and head_sha in {event.sha, event.head_sha}:
        return True
    if event.mode != MODE_REHEARSAL or kind != "pull_request" or head_sha != event.head_sha:
        return False
    numbers = [p.get("number") for p in run.get("pull_requests") or [] if isinstance(p, dict)]
    if numbers:
        return event.pr_number in numbers
    return event.head_ref is not None and run.get("head_branch") == event.head_ref


def ci_verdict(api: Api, repository: str, event: ReleaseEvent) -> tuple[str, dict[str, Any] | None]:
    """``success`` (with the run), ``pending``, ``failed`` (with a run) or ``none``."""
    shas = sorted({event.sha, event.head_sha})
    runs = [
        r
        for sha in shas
        for r in _runs(api, f"repos/{repository}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={sha}&per_page=100")
        if _accepted(r, event, repository)
    ]
    ok = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success"]
    if ok:
        return "success", max(ok, key=lambda r: int(r.get("id") or 0))
    if any(r.get("status") in IN_PROGRESS_STATUSES for r in runs):
        return "pending", None
    if runs:
        return "failed", max(runs, key=lambda r: int(r.get("id") or 0))
    return "none", None


def wait_for_ci(
    api: Api,
    repository: str,
    event: ReleaseEvent,
    *,
    wait_seconds: int,
    poll_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    deadline = clock() + wait_seconds
    what = f"{CI_WORKFLOW} on {event.sha}" + (f" (PR head {event.head_sha})" if event.head_sha != event.sha else "")
    while True:
        verdict, run = ci_verdict(api, repository, event)
        if verdict == "success" and run is not None:
            print(f"CI passed: {run.get('html_url')}")
            return run
        if verdict == "failed" and run is not None:
            raise ReleaseError(f"CI did not pass for {what}: {run.get('conclusion')} ({run.get('html_url')})")
        if clock() >= deadline:
            raise ReleaseError(f"no passed {what} within {wait_seconds}s ({verdict}); re-run once CI has passed")
        print(f"::notice::waiting for {what} ({'running' if verdict == 'pending' else 'not started'})")
        sleep(poll_seconds)


def check_tag_not_moved(api: Api, inp: GateInputs, event: ReleaseEvent) -> None:
    tag = event.version.tag
    if tag is None:
        return
    workflow = _workflow_path(inp.workflow_ref, inp.repository).rsplit("/", 1)[1]
    # parse_tag is case-insensitive (``2.5.0-rc1`` and ``2.5.0-RC1`` are one rpm), but GitHub's branch
    # filter is exact: ask for every case variant of the tag (letters are only ``v`` and ``rc``).
    moved: set[str] = set()
    for variant in _case_variants(tag):
        path = f"repos/{inp.repository}/actions/workflows/{workflow}/runs?branch={variant}&event=push&per_page=100"
        data = api(path)
        if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
            raise ReleaseError(f"cannot list earlier runs of {workflow} for tag {variant}: refusing to release blind")
        for r in data["workflow_runs"]:
            branch = r.get("head_branch") if isinstance(r, dict) else None
            if isinstance(branch, str) and branch.lower() == tag.lower() and r.get("head_sha") != event.sha:
                moved.add(f"{branch}@{r.get('head_sha')}")
    if moved:
        raise ReleaseError(
            f"tag {tag} (or a case variant of it) was released from other commit(s) {sorted(moved)} before, "
            f"now {event.sha}: tags are immutable; tag a new rc"
        )


def _case_variants(tag: str) -> list[str]:
    """Every upper/lower-case spelling of ``tag`` (``2.5.0-rc1`` -> 4 spellings)."""
    choices = [sorted({c.lower(), c.upper()}) for c in tag]
    return ["".join(p) for p in itertools.product(*choices)]


def resolve_release_ref(api: Api, repo: str, ref: str) -> str:
    if _SHA_RE.fullmatch(ref):
        data = api(f"repos/{repo}/commits/{ref}")
        if not isinstance(data, dict) or data.get("sha") != ref:
            raise ReleaseError(f"release-ref {ref} is not a commit of {repo}")
        return ref
    data = api(f"repos/{repo}/git/ref/tags/{ref}")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        raise ReleaseError(
            f"release-ref {ref!r} is not a tag of {repo}: a release builds a dep at a tag or SHA, never a branch"
        )
    for _ in range(5):
        if obj.get("type") == "commit" and isinstance(obj.get("sha"), str) and _SHA_RE.fullmatch(obj["sha"]):
            return str(obj["sha"])
        if obj.get("type") != "tag":
            break
        data = api(f"repos/{repo}/git/tags/{obj.get('sha')}")
        obj = data.get("object") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            break
    raise ReleaseError(f"release-ref {ref!r} of {repo} does not resolve to a commit")


def _probe(read_urls: Mapping[str, str], allow_production: bool, auth: str = "") -> Probe:
    if not auth:
        print(
            f"::warning::no Nexus credential (${nexus.AUTH_ENV}): probing anonymously; a private repository "
            "cannot be probed, so a published leg is rebuilt and upload-nexus refuses its new bytes"
        )

    def probe(target: str, filename: str) -> str:
        base = nexus.target_url(read_urls, target, allow_production=allow_production)
        return nexus.probe(base, filename, auth=auth)

    return probe


def run_gate(
    inp: GateInputs,
    *,
    api: Api,
    probe: Probe | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> GateResult:
    # 1
    if inp.kill_switch.strip().lower() == KILL_SWITCH_ON:
        raise ReleaseError(f"CD kill switch is on: {inp.kill_switch_reason or '(no reason given)'}")
    rm = legs.load(inp.manifest)
    meta = rm.raw.release
    assert meta is not None
    # 2
    project_version = read_project_version(inp.repo_root)
    event = classify(
        event_name=inp.event_name,
        ref=inp.ref,
        sha=inp.sha,
        repository=inp.repository,
        payload=inp.payload,
        project_version=project_version,
        tag_prefix=meta.tag_prefix,
        version_parts=meta.version_parts,
    )
    print(f"mode: {event.mode}; commit {event.sha}" + (f"; tag {event.version.tag}" if event.version.tag else ""))
    # 3
    if event.mode == MODE_RELEASE and not inp.allow_production:
        raise ReleaseError(f"tag {event.version.tag} is a release; this pipeline publishes prereleases (-rcN) only")
    # 4
    if event.version.tag is not None:
        check_tag_matches_version(event.version, project_version)
    # 5
    ci_run = wait_for_ci(
        api,
        inp.repository,
        event,
        wait_seconds=inp.ci_wait_seconds,
        poll_seconds=inp.poll_seconds,
        sleep=sleep,
        clock=clock,
    )
    # 6
    check_tag_not_moved(api, inp, event)
    # 7
    deps = [{**d, "sha": resolve_release_ref(api, d["repo"], d["ref"])} for d in legs.source_deps(rm)]
    # 8
    workflow_file = inp.repo_root / _workflow_path(inp.workflow_ref, inp.repository)
    if problems := check_cd_workflow.check(workflow_file, allow_refs=inp.allow_refs):
        raise ReleaseError("the release workflow breaks the rules check-cd-workflow enforces:\n" + "\n".join(problems))
    # 9
    expanded = legs.expand(rm, inp.kinds, event, allow_production=inp.allow_production)
    # 10
    if event.mode != MODE_REHEARSAL:
        probe = probe or _probe(inp.nexus_read_urls, inp.allow_production, inp.nexus_auth)
        for kind_legs in expanded.values():
            for i, leg in enumerate(kind_legs):
                ctx = leg["_release"]
                if ctx["channel"] == "rpm" and probe(ctx["nexus_target"], ctx["filename"]) == "present":
                    print(f"::notice::{ctx['filename']} is on Nexus already: leg {ctx['leg_id']} is not rebuilt")
                    kind_legs[i] = {**leg, "_release": {**ctx, "state": legs.STATE_ALREADY_PUBLISHED}}
    provenance = {
        "repository": inp.repository,
        "run_url": f"{inp.server_url}/{inp.repository}/actions/runs/{inp.run_id}",
        "workflow_ref": inp.workflow_ref,
        "action_ref": inp.action_ref,
        "mode": event.mode,
        "tag": event.version.tag,
        "version": event.version.base,
        "sha": event.sha,
        "head_sha": event.head_sha,
        "pr_number": event.pr_number,
        "ci_run": {"id": ci_run.get("id"), "url": ci_run.get("html_url"), "event": ci_run.get("event")},
        "source_deps": deps,
        "containers": sorted(
            {str(leg.get(k)) for kl in expanded.values() for leg in kl for k in ("container", "install-test-image")}
        ),
        "legs": {
            kind: [{"leg_id": leg["_release"]["leg_id"], "state": leg["_release"]["state"]} for leg in kl]
            for kind, kl in expanded.items()
        },
    }
    return GateResult(event=event, legs=expanded, source_deps=deps, provenance=provenance)


@click.command("gate")
@click.option("--manifest", type=click.Path(path_type=Path), default=Path(".ci/manifest.toml"), show_default=True)
@click.option("--kinds", required=True, help="Comma-separated release kinds, e.g. release-rpm.")
@click.option("--allow-production", is_flag=True, help="Allow release mode and production targets.")
@click.option("--nexus-read-urls", default="{}", show_default=True, help="JSON: nexus-target -> repository URL.")
@click.option("--ci-wait-seconds", type=int, default=7200, show_default=True)
@click.option("--poll-seconds", type=int, default=60, show_default=True)
@click.option("--allow-ref", "allow_refs", multiple=True, help="As check-cd-workflow --allow-ref.")
def main(
    manifest: Path,
    kinds: str,
    allow_production: bool,
    nexus_read_urls: str,
    ci_wait_seconds: int,
    poll_seconds: int,
    allow_refs: tuple[str, ...],
) -> None:
    """Run the release gate for this workflow run (GITHUB_* env); write its outputs.

    The Nexus credential for the probe comes from $NEXUS_AUTH (user:token), never an option.
    """
    env = os.environ
    nexus_auth = nexus.take_auth()
    event_path = env.get("GITHUB_EVENT_PATH")
    payload = json.loads(Path(event_path).read_text()) if event_path else {}
    urls = nexus.parse_urls(nexus_read_urls, what="--nexus-read-urls")
    repo_root = manifest.resolve().parents[1]
    inp = GateInputs(
        repo_root=repo_root,
        manifest=manifest,
        kinds=[k.strip() for k in kinds.split(",") if k.strip()],
        repository=env.get("GITHUB_REPOSITORY", ""),
        event_name=env.get("GITHUB_EVENT_NAME", ""),
        ref=env.get("GITHUB_REF", ""),
        sha=env.get("GITHUB_SHA", ""),
        payload=payload,
        workflow_ref=env.get("GITHUB_WORKFLOW_REF", ""),
        run_id=env.get("GITHUB_RUN_ID", ""),
        server_url=env.get("GITHUB_SERVER_URL", "https://github.com"),
        action_ref=env.get("CI_RELEASE_ACTION_REF", ""),
        allow_production=allow_production,
        nexus_read_urls={str(k): str(v) for k, v in urls.items()},
        ci_wait_seconds=ci_wait_seconds,
        poll_seconds=poll_seconds,
        kill_switch=env.get("CD_KILL_SWITCH", ""),
        kill_switch_reason=env.get("CD_KILL_SWITCH_REASON", ""),
        allow_refs=allow_refs,
        nexus_auth=nexus_auth,
    )
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    result = run_gate(inp, api=lambda path: gh_api_rest(path, token))
    write_outputs(result.outputs())
