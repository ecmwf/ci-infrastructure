#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Download each resolved dep (`_resolved.deps`) into its install path.

An uncached dep is re-polled while an upstream run is in flight (ARTIFACT_WAIT_TIMEOUT,
ARTIFACT_POLL_INTERVAL). The tar.gz stays at $RUNNER_TEMP/<artifact-name>.tar.gz for re-upload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Final, Literal, TypedDict

import click

from . import failure_reasons, s3_store
from ._errors import CIError, escape_command_data
from ._github_api import IN_PROGRESS_STATUSES, WorkflowRuns, probe_workflow_runs, select_token, write_outputs

_DEFAULT_POLL_INTERVAL: Final = 60
_DEFAULT_WAIT_TIMEOUT: Final = 1800


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def _fmt_duration(seconds: float) -> str:
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _run_phase(detail: str | None) -> str:
    if detail == "in_progress":
        return "building"
    if detail in IN_PROGRESS_STATUSES:
        return "queued/scheduling"
    return detail or "in progress"


class Dep(TypedDict):
    name: str
    repo: str
    ref: str
    sha: str
    artifact_name: str
    cached: bool
    source: str
    install_path: Path


def _diagnose_missing_artifact(name: str, repo: str, sha: str, run: WorkflowRuns, token: str | None) -> None:
    if run.state == "completed":
        where = f" Run: {run.url}" if run.url else ""
        reasons = failure_reasons.explain(run.url, token) if run.conclusion == "failure" else ""
        message = f"Upstream CI for {repo}@{sha[:8]} completed ({run.conclusion}) without publishing '{name}'.{where}"
        print(f"::error::{escape_command_data(message + reasons)}", file=sys.stderr)
        use = "Re-run that upstream run, then use" if run.conclusion == "failure" else "Use"
        print(
            f"::warning::{use} 'Re-run all jobs' on this workflow, not 'Re-run failed jobs': "
            "which builds to wait for was decided when this run started, and re-running only "
            "the failed jobs waits for the same ones again. If it still fails, the consumer's manifest "
            "matrix may ask for a (compiler, build-type, python-version, platform) combination "
            "the upstream repo never built.",
            file=sys.stderr,
        )
    else:
        print(
            f"::warning::No workflow runs were found for {repo}@{sha[:8]} — nothing has built '{name}' yet.",
            file=sys.stderr,
        )
        print(
            "::warning::Check that the upstream repo has CI configured for this ref, that it actually ran, "
            "and that the consumer is not pointing at a stale or unreachable SHA.",
            file=sys.stderr,
        )


def poll_for_artifact(repo: str, sha: str, name: str, token: str | None) -> bool:
    """True once the artifact is in the store; False when no upstream run is in flight or on timeout."""
    poll_interval = int(os.environ.get("ARTIFACT_POLL_INTERVAL", _DEFAULT_POLL_INTERVAL))
    wait_timeout = int(os.environ.get("ARTIFACT_WAIT_TIMEOUT", _DEFAULT_WAIT_TIMEOUT))
    start = time.monotonic()
    deadline = start + wait_timeout
    announced_wait = False
    last_phase = "in progress"
    last_url: str | None = None

    while True:
        if s3_store.object_exists(name):
            if announced_wait:
                print(f"  ✓ {name} is now available after waiting {_fmt_duration(time.monotonic() - start)} for {repo}")
            return True

        run = probe_workflow_runs(repo, sha, token)
        state = run.state
        if state == "running":
            remaining = deadline - time.monotonic()
            phase = _run_phase(run.detail)
            last_phase = phase
            last_url = run.url or last_url
            if remaining <= 0:
                where = f" Run: {last_url}" if last_url else ""
                print(
                    f"::warning::Timed out after {wait_timeout}s waiting for {name} (repo={repo}, sha={sha[:8]}). "
                    f"Upstream run is still {last_phase}.{where}",
                    file=sys.stderr,
                )
                return False
            if not announced_wait:
                where = f" Run: {run.url}" if run.url else ""
                print(
                    f"::notice::Blocked on upstream: {repo}@{sha[:8]} run is {phase} "
                    f"('{name}'). This job WAITS until it finishes (up to {_fmt_duration(wait_timeout)}).{where}"
                )
                announced_wait = True
            print(
                f"  ⏳ waiting on {repo}@{sha[:8]} ({phase}) for {name} — "
                f"{_fmt_duration(time.monotonic() - start)} elapsed, "
                f"{_fmt_duration(remaining)} left until timeout; next check in {poll_interval}s"
            )
            time.sleep(poll_interval)
            continue

        _diagnose_missing_artifact(name, repo, sha, run, token)
        return False


def download_from_store(artifact_name: str, install_path: Path) -> Path | None:
    """Download and extract the artifact; returns the tar left in $RUNNER_TEMP, or None on failure."""
    tar_dst = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{artifact_name}.tar.gz"
    install_path.mkdir(parents=True, exist_ok=True)
    if not s3_store.download(artifact_name, tar_dst):
        return None
    if subprocess.run(["tar", "-xzf", tar_dst, "-C", str(install_path)]).returncode != 0:
        return None
    return tar_dst


def _download_and_report(dep: Dep) -> bool:
    start = time.monotonic()
    tar = download_from_store(dep["artifact_name"], dep["install_path"])
    if tar is None:
        return False
    print(
        f"  downloaded {dep['artifact_name']} ({_human_bytes(tar.stat().st_size)}) "
        f"in {_fmt_duration(time.monotonic() - start)}"
    )
    return True


def fetch_one(dep: Dep, token: str | None) -> Literal["artifact", "artifact-after-wait", "fail"]:
    source: Literal["artifact", "artifact-after-wait"] | None = None
    if dep["cached"]:
        if _download_and_report(dep):
            source = "artifact"
        else:
            print(
                f"::warning::download_from_store failed for {dep['artifact_name']} "
                f"(repo={dep['repo']}); re-polling the store",
                file=sys.stderr,
            )
    if source is None:
        print(
            f"  {dep['name']}: no cached artifact yet — checking the store for "
            f"'{dep['artifact_name']}' (will wait if {dep['repo']} is building it)"
        )
        if poll_for_artifact(dep["repo"], dep["sha"], dep["artifact_name"], token) and _download_and_report(dep):
            source = "artifact-after-wait"

    return source or "fail"


@click.command(help="Fetch each resolved dep into its install path.")
@click.option("--deps-json", "deps_json", required=True, help="JSON array of resolved deps")
def main(deps_json: str) -> None:
    # Flushed first, to tell an import hang from a fetch hang.
    print("fetch_deps: starting", flush=True)

    raw = json.loads(deps_json)
    if not isinstance(raw, list):
        raise CIError("--deps-json must be a JSON array")

    unresolved = [e for e in raw if isinstance(e, dict) and e.get("source") == "unresolved"]
    if unresolved:
        raise CIError(
            "\n".join(str(e.get("error") or f"dep {e.get('name')} could not be resolved") for e in unresolved)
        )

    if not raw:
        print("fetch_deps: no deps to fetch for this leg; nothing to do.")
        write_outputs({"updated-deps-json": json.dumps(raw)})
        return

    missing_tools = [t for t in ("gh", "tar") if shutil.which(t) is None]
    if missing_tools:
        raise CIError(
            f"fetch_deps requires the following CLI tool(s) but none were found on PATH: "
            f"{', '.join(missing_tools)}. Install them in this runner/image and retry."
        )

    token = select_token()
    failures: list[str] = []
    source_counts: dict[str, int] = {}
    run_start = time.monotonic()

    ready = [e["name"] for e in raw if isinstance(e, dict) and e.get("cached")]
    pending = [e["name"] for e in raw if isinstance(e, dict) and not e.get("cached")]
    print(f"fetch_deps: {len(raw)} dep(s) to fetch.")
    if ready:
        print(f"  ready to download (cached upstream): {', '.join(ready)}")
    if pending:
        print(f"  not cached yet, will re-check and may wait on an upstream build: {', '.join(pending)}")

    for entry in raw:
        dep: Dep = {
            "name": entry["name"],
            "repo": entry["repo"],
            "ref": entry["ref"],
            "sha": entry["sha"],
            "artifact_name": entry["artifact-name"],
            "cached": bool(entry.get("cached", False)),
            "source": entry["source"],
            "install_path": Path(os.path.expandvars(entry["install-path"])),
        }
        result = fetch_one(dep, token)
        if result == "fail":
            failures.append(dep["artifact_name"])
            print(
                f"::error::Could not fetch dep {dep['name']} (artifact {dep['artifact_name']}) — "
                f"resolver source={dep['source']!r}, cached={dep['cached']!r}, "
                f"repo={dep['repo']!r}. Not in the artifact store.",
                file=sys.stderr,
            )
        else:
            entry["source"] = "artifact"
            source_counts[result] = source_counts.get(result, 0) + 1
            print(f"Fetched {dep['name']} from {result}: {dep['install_path']}")

    total = _fmt_duration(time.monotonic() - run_start)
    cached = source_counts.get("artifact", 0)
    after_wait = source_counts.get("artifact-after-wait", 0)
    summary = f"fetch_deps done in {total}: {cached} downloaded directly"
    if after_wait:
        summary += f", {after_wait} downloaded after waiting on an upstream build"
    if failures:
        summary += f", {len(failures)} failed"
    print(summary + ".")

    if failures:
        raise CIError(f"{len(failures)} dep(s) could not be fetched.")

    write_outputs({"updated-deps-json": json.dumps(raw)})


if __name__ == "__main__":
    main()
