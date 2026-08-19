#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
fetch_deps.py

Downloads each dep listed in a resolved-deps JSON blob (from resolve_deps.py)
into its canonical install path. For each dep:

  1. If `cached` is set, download the artifact's object from the S3 store by
     name and extract it.
  2. Else, re-query the S3 store by name. The resolver may have run before the
     upstream CI uploaded its artifact; this catches the race where upstream
     finished between resolve and fetch. If a workflow run for the upstream SHA
     is in progress, poll until it completes (up to ARTIFACT_WAIT_TIMEOUT
     seconds, default 1800; ARTIFACT_POLL_INTERVAL between polls, default 60).
  3. Else, fail with a clear diagnostic.

Each download is extracted into <install-path>. The tar.gz file is also left at
$RUNNER_TEMP/<artifact-name>.tar.gz so a downstream `publish-package` step can
re-upload it without recompressing.

Two interpreters are deliberately kept separate:

  * The "infrastructure" Python (`$CI_INFRASTRUCTURE_PYTHON`, a dedicated venv
    materialised by `ensure-infrastructure-present`) runs THIS script. It
    holds the ci-infrastructure library and nothing else.
  * The "consumer" Python (passed in via `--consumer-python`) is the test
    interpreter — the one set up by actions/setup-python on the consuming
    job, exported as `$pythonLocation/bin/python`. `needs-python` wheels are
    installed into THIS interpreter so the consumer's pytest can import them.

There is no fallback between the two: if a `needs-python` dep is fetched
without `--consumer-python`, we fail loud. Silently falling back to
`sys.executable` (the infra venv) would either land the wheel in a
site-packages pytest never sees (wrong-Python bug) or get rejected outright
when the wheel's Python tag mismatches the venv's version.

`--no-python-install` is the one way to fetch `needs-python` deps with no
consumer interpreter at all. It stages each wheel in its install path without
installing it anywhere, which is what an HPC leg needs: the runner only ships
the wheel dirs to the cluster, and the repo's job script installs them into a
module-loaded interpreter on the compute node.

Usage:
    fetch_deps.py --deps-json '<JSON list of dep objects>' \\
                  [--consumer-python /path/to/test-interpreter/bin/python] \\
                  [--no-python-install]

The JSON must be the `_resolved.deps` array from resolve_deps.py output:
    [{name, repo, ref, sha, artifact-name, cached, source, install-path}, ...]
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

from . import s3_store
from ._errors import CIError
from ._github_api import IN_PROGRESS_STATUSES, probe_workflow_runs, select_token, write_outputs

_DEFAULT_POLL_INTERVAL: Final = 60
_DEFAULT_WAIT_TIMEOUT: Final = 1800


def _human_bytes(n: int) -> str:
    """Format a byte count as a short human-readable string (e.g. '122.5 MiB')."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def _fmt_duration(seconds: float) -> str:
    """Format a duration as 'Ms Ns' or just 'Ns' for sub-minute spans."""
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _run_phase(detail: str | None) -> str:
    """Human phrase for a GitHub run status, so the wait log says whether time
    goes to the runner queue/scheduling or to the actual build."""
    if detail == "in_progress":
        return "building"
    if detail in IN_PROGRESS_STATUSES:  # queued / waiting / requested / pending
        return "queued/scheduling"
    return detail or "in progress"


class Dep(TypedDict):
    name: str
    repo: str
    ref: str
    sha: str
    artifact_name: str
    cached: bool  # True when the resolver found the artifact in the store
    source: str
    install_path: Path
    needs_python: bool


def _diagnose_missing_artifact(
    name: str, repo: str, sha: str, token: str | None, status: Literal["completed", "none"]
) -> None:
    """Emit a structured diagnostic when poll_for_artifact gives up."""
    prefix = name.split("-", 1)[0] if "-" in name else name
    available = s3_store.list_with_prefix(prefix)
    lines: list[str] = []
    if status == "completed":
        lines.append(
            f"Upstream CI for {repo}@{sha[:8]} has completed but no artifact named '{name}' exists in the store."
        )
        lines.append(
            "Re-trigger the upstream CI to rebuild it, then re-run this workflow. "
            "Alternatively, the consumer's manifest matrix may ask for a (compiler, build-type, "
            "python-version, os) combination the upstream repo never built."
        )
    elif status == "none":
        lines.append(f"No workflow runs were found for {repo}@{sha[:8]} — nothing has built '{name}' yet.")
        lines.append(
            "Check that the upstream repo has CI configured for this ref, that it actually ran, "
            "and that the consumer is not pointing at a stale or unreachable SHA."
        )
    else:
        lines.append(f"Could not resolve '{name}' in {repo} (status={status}).")

    if available:
        lines.append(f"Artifacts in the store matching prefix '{prefix}-':")
        for n in available:
            lines.append(f"  - {n}")
        lines.append(
            "Compare the (compiler, build-type, python-version, os) bits in those names against "
            "the consumer manifest's [[matrix.<job>.include]] rows; mismatches are usually a "
            "leftover entry referencing an unsupported toolchain."
        )
    else:
        lines.append(f"No artifacts with prefix '{prefix}-' currently exist in the store.")

    # GitHub renders ::warning:: as a single line; emit individual warnings so every line is visible.
    for line in lines:
        print(f"::warning::{line}", file=sys.stderr)


def poll_for_artifact(repo: str, sha: str, name: str, token: str | None) -> bool:
    """
    Poll the artifact store until the named artifact appears or the upstream CI
    gives up.

    Returns True once the artifact is present, or False if we should stop trying.
    """
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
                # Loud, one-time banner so the reason for a long job is obvious in
                # the log rather than buried in a 60s heartbeat. The phase tells
                # whether time goes to the runner queue/scheduling or the build.
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

        # state == 'completed' or 'none' — no point polling further.
        _diagnose_missing_artifact(name, repo, sha, token, state)
        return False


def download_from_store(artifact_name: str, install_path: Path) -> bool:
    """Download the artifact's tar.gz from the S3 store and extract to install_path.

    The stored object is the tar.gz directly (no zip wrapper). The tar is left
    at $RUNNER_TEMP/<artifact-name>.tar.gz so a downstream publish step can
    re-upload it without recompressing.
    """
    runner_tmp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    install_path.mkdir(parents=True, exist_ok=True)

    tar_dst = runner_tmp / f"{artifact_name}.tar.gz"
    if not s3_store.download(artifact_name, tar_dst):
        return False

    return subprocess.run(["tar", "-xzf", tar_dst, "-C", str(install_path)]).returncode == 0


def pip_install_wheel(install_path: Path, consumer_python: Path) -> bool:
    """If install_path contains exactly one .whl, pip-install it into the
    consumer's interpreter. Return True on success.

    `consumer_python` is the absolute path to the test-runner's Python. It is
    intentionally distinct from `sys.executable` (the ci-infrastructure helper
    venv) — see the module docstring.
    """
    if not install_path.is_dir():
        return False
    whls = [f.name for f in install_path.iterdir() if f.name.endswith(".whl")]
    if not whls:
        print(f"::warning::needs-python dep at {install_path} contained no .whl file", file=sys.stderr)
        return False
    if len(whls) > 1:
        print(
            f"::warning::needs-python dep at {install_path} contained multiple .whl files; "
            f"installing only the first: {whls[0]}",
            file=sys.stderr,
        )
    wheel_path = install_path / whls[0]
    # --break-system-packages disarms PEP 668's externally-managed marker
    # that ships on Debian/Ubuntu Python ≥ 3.11; the runner is disposable
    # so "breaking" its Python is a non-event, and the flag is a silent
    # no-op when no marker is present (e.g. setup-python interpreters).
    rc = subprocess.run(
        [
            str(consumer_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            "--break-system-packages",
            str(wheel_path),
        ]
    ).returncode
    return rc == 0


def _download_and_report(dep: Dep) -> bool:
    """Download a dep's artifact and, on success, log how big it was and how long
    it took — so a slow job can be attributed to a large transfer vs. a long wait."""
    runner_tmp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    start = time.monotonic()
    ok = download_from_store(dep["artifact_name"], dep["install_path"])
    if ok:
        tar = runner_tmp / f"{dep['artifact_name']}.tar.gz"
        size = tar.stat().st_size if tar.is_file() else 0
        print(
            f"  downloaded {dep['artifact_name']} ({_human_bytes(size)}) in {_fmt_duration(time.monotonic() - start)}"
        )
    return ok


def fetch_one(
    dep: Dep, token: str | None, consumer_python: Path | None
) -> Literal["artifact", "artifact-after-wait", "fail"]:
    """Returns the actual source ('artifact'|'artifact-after-wait'|'fail').

    `consumer_python` is required when the dep is `needs-python`, unless the
    caller passed --no-python-install, in which case it is None and the wheel is
    left staged in its install path. main() validates both up front, so by the
    time we get here None means "stage only" and never "forgot to pass it".
    """
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
        # Resolver didn't find the artifact at resolve time, or the download
        # failed mid-flight. The upstream CI may still be running (race: cxx-py's
        # resolve job ran while cxx's build was mid-upload, or resolve_deps just
        # dispatched a rebuild of this producer). Re-query the store and poll if
        # upstream is in-progress.
        print(
            f"  {dep['name']}: no cached artifact yet — checking the store for "
            f"'{dep['artifact_name']}' (will wait if {dep['repo']} is building it)"
        )
        if poll_for_artifact(dep["repo"], dep["sha"], dep["artifact_name"], token) and _download_and_report(dep):
            source = "artifact-after-wait"

    if source is None:
        return "fail"

    # Python-aware deps ship a wheel inside the artifact; pip-install it into
    # the consumer's interpreter so the consumer's tests can `import <package>`
    # without LD_LIBRARY_PATH/PYTHONPATH.
    if dep["needs_python"] and consumer_python is not None:
        if not pip_install_wheel(dep["install_path"], consumer_python):
            print(
                f"::error::pip install failed for python-aware dep {dep['name']} "
                f"(install path: {dep['install_path']}, consumer-python: {consumer_python})",
                file=sys.stderr,
            )
            return "fail"
    return source


@click.command(help="Fetch each resolved dep into its install path.")
@click.option("--deps-json", "deps_json", required=True, help="JSON array of resolved deps")
@click.option(
    "--consumer-python",
    "consumer_python_arg",
    default=None,
    help=(
        "Absolute path to the consumer's Python interpreter (the one running pytest). "
        "Required when any dep has needs-python=true; ignored otherwise. The fetch-and-publish "
        "action populates this from $pythonLocation/bin/python (set by actions/setup-python)."
    ),
)
@click.option(
    "--python-install/--no-python-install",
    "python_install",
    default=True,
    help=(
        "Whether to pip-install needs-python deps' wheels into --consumer-python. "
        "--no-python-install stages each wheel in its install path and installs nothing, "
        "for HPC legs whose job script installs them on the compute node instead."
    ),
)
def main(deps_json: str, consumer_python_arg: str | None, python_install: bool) -> None:
    # Unbuffered marker as the very first action: if this line shows in the log
    # but nothing else does, the hang is below (preflight/fetch); if it never
    # shows, the hang is in interpreter startup/imports, not this function.
    print("fetch_deps: starting", flush=True)

    raw = json.loads(deps_json)
    if not isinstance(raw, list):
        raise CIError("--deps-json must be a JSON array")

    # No deps → nothing to fetch. Return before the gh/unzip/tar preflight and
    # token lookup so a leaf package (e.g. the root Fortran library, whose deps
    # list is empty) finishes instantly without probing PATH at all.
    if not raw:
        print("fetch_deps: no deps to fetch for this leg; nothing to do.")
        write_outputs({"updated-deps-json": json.dumps(raw)})
        return

    # Preflight: fetch_deps shells out to `gh` (upstream run-state probe) and
    # `tar` (extract the downloaded artifact). Missing either surfaces deep in
    # the fetch path as a Python FileNotFoundError traceback; check up front so
    # the message points at the image, not at the script.
    missing_tools = [t for t in ("gh", "tar") if shutil.which(t) is None]
    if missing_tools:
        raise CIError(
            f"fetch_deps requires the following CLI tool(s) but none were found on PATH: "
            f"{', '.join(missing_tools)}. Install them in this runner/image and retry."
        )

    # Preflight: if any dep needs a Python wheel install, the consumer interpreter
    # must be passed in. Refuse to silently fall back to sys.executable (the
    # ci-infrastructure helper venv) — that path either installs into the wrong
    # site-packages (consumer's pytest doesn't see the package) or fails outright
    # when the helper venv's Python version doesn't match the wheel tag. Opting
    # out of the install entirely (--no-python-install) skips the requirement:
    # there is then no interpreter here to be wrong about.
    needs_python_deps = [e["name"] for e in raw if isinstance(e, dict) and e.get("needs-python")]
    consumer_python: Path | None = None
    if needs_python_deps and not python_install:
        print(
            f"fetch_deps: staging needs-python wheels ({', '.join(needs_python_deps)}) without "
            "installing them — --no-python-install was passed. Whoever consumes them (on an HPC "
            "leg, the job script on the compute node) installs them from their install paths."
        )
    elif needs_python_deps:
        if not consumer_python_arg:
            raise CIError(
                "fetch_deps was asked to install needs-python wheels "
                f"({', '.join(needs_python_deps)}) but no --consumer-python was provided. "
                "Run actions/setup-python before fetch-and-publish so $pythonLocation is set, "
                "or pass --consumer-python explicitly. On a leg that installs the wheels "
                "elsewhere (e.g. an HPC job script on the compute node), pass "
                "install-python-deps: 'false' to fetch-and-publish instead."
            )
        consumer_python = Path(consumer_python_arg)
        if not consumer_python.is_file():
            raise CIError(
                f"--consumer-python={consumer_python} does not point to an existing file. "
                "Check that actions/setup-python ran in this job and that $pythonLocation resolved correctly."
            )

    token = select_token()
    failures: list[str] = []
    source_counts: dict[str, int] = {}
    run_start = time.monotonic()

    # Upfront plan so the log states what this step will do before it blocks on
    # anything: cached deps download immediately; the rest must re-query the
    # store and may WAIT for an upstream (re)build to finish.
    ready = [e["name"] for e in raw if isinstance(e, dict) and e.get("cached")]
    pending = [e["name"] for e in raw if isinstance(e, dict) and not e.get("cached")]
    print(f"fetch_deps: {len(raw)} dep(s) to fetch.")
    if ready:
        print(f"  ready to download (cached upstream): {', '.join(ready)}")
    if pending:
        print(f"  not cached yet, will re-check and may wait on an upstream build: {', '.join(pending)}")

    for entry in raw:
        # Translate hyphenated keys from JSON to snake_case for our TypedDict.
        dep: Dep = {
            "name": entry["name"],
            "repo": entry["repo"],
            "ref": entry["ref"],
            "sha": entry["sha"],
            "artifact_name": entry["artifact-name"],
            "cached": bool(entry.get("cached", False)),
            "source": entry["source"],
            # The resolver writes install-path as `$RUNNER_TEMP/install/<name>`
            # — a template, not an absolute path — because the resolver may run
            # in a different runner (e.g. ARC container) than this fetcher
            # (e.g. ubuntu-latest host). expandvars resolves it against the
            # local job's RUNNER_TEMP.
            "install_path": Path(os.path.expandvars(entry["install-path"])),
            "needs_python": bool(entry.get("needs-python", False)),
        }
        result = fetch_one(dep, token, consumer_python)
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
