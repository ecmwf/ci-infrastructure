# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Hold a CD v3 release workflow (`.github/workflows/cd-v3.yml`) to the shape that keeps credentials safe.

Experimental; see docs/configuring/releases.rst. The release gate runs the same check on the
workflow file of the commit it releases, so the rules hold in CI, not only in pre-commit.

- I1: runs on tag pushes, `pull_request: [labeled]`, `workflow_dispatch` or `schedule` only.
- I2: workflow permissions at most `contents: read`.
- I3: a `concurrency` group on the ref, never cancelled in progress.
- I4: no `secrets: inherit`, no workflow `env`, nothing of the CI caches (AWS_*, ARTIFACT_S3_*, SCCACHE_*).
- I5: a secret outside the CI allowlist only in a job with an `environment`.
- I6: an environment job runs no `run:` step, checks out nothing, builds nothing; uses only
  artifact transfer and the publish actions; on an ECMWF runner, in an ECCR container; sets no
  `env` that changes process startup (LD_*, BASH_ENV, PATH, PYTHON*, GIT_*, ...); downloads only
  this run's artifacts (no `run-id`, `repository`, `github-token`).
- I7: an environment job needs every build and test job, and runs exactly
  `if: ${{ !cancelled() && needs.gate.outputs.publish == 'true' && !contains(needs.*.result, 'failure') }}`.
- I8: no `${{ }}` inside a `run:` script; pass values through `env`.
- I9: one ref for every ci-infrastructure action, a commit SHA unless allowed (`--allow-ref`);
  every other action pinned by commit SHA.
- I10: every job but `ci-approval` needs it directly; every job but those two needs `gate` directly.
- Every `actions/upload-artifact` sets `overwrite: true`, so re-running a job can replace its upload.

Imports only click and yaml, so the pre-commit hook needs nothing else.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import click
import yaml

GATE_JOB: Final = "ci-approval"
RELEASE_GATE_JOB: Final = "gate"
#: Not a build or test job: it summarises, and runs after the environment jobs.
RECORD_JOB: Final = "record"

CI_INFRA: Final = "ecmwf/ci-infrastructure/"
APPROVAL_ACTION: Final = "ecmwf/ci-infrastructure/actions/require-ci-approval"
UPLOAD_ARTIFACT: Final = "actions/upload-artifact"
DOWNLOAD_ARTIFACT: Final = "actions/download-artifact"
CHECKOUT: Final = "actions/checkout"

#: What an environment job may run.
ENVIRONMENT_ACTIONS: Final = (
    UPLOAD_ARTIFACT,
    DOWNLOAD_ARTIFACT,
    "ecmwf/ci-infrastructure/actions/upload-nexus",
    "ecmwf/ci-infrastructure/actions/hpc-promote",
    "ecmwf/ci-infrastructure/actions/hpc-sync-tag",
)
#: Only an environment job may run these: they publish.
PUBLISH_ACTIONS: Final = ENVIRONMENT_ACTIONS[2:]
#: Build the release; never next to a publish credential.
BUILD_ACTIONS: Final = (
    "ecmwf/ci-infrastructure/actions/run-job-script",
    "ecmwf/ci-infrastructure/actions/build-on-hpc",
    "ecmwf/ci-infrastructure/actions/release-source-deps",
)

#: `env` names an environment job may not set: they change how its processes start.
_STARTUP_ENV: Final = re.compile(
    r"^(BASH_ENV|ENV|LD_[A-Z0-9_]+|PATH|NODE_OPTIONS|NODE_PATH|PYTHON[A-Z0-9_]*|GIT_[A-Z0-9_]*"
    r"|PERL5OPT|PERL5LIB|PERLLIB|RUBYOPT|RUBYLIB)$",
    re.I,
)
#: download-artifact inputs that fetch from another run or repository; refused in an environment job.
_FOREIGN_DOWNLOAD_INPUTS: Final = ("run-id", "repository", "github-token")

ENVIRONMENT_JOB_IF: Final = (
    "!cancelled() && needs.gate.outputs.publish == 'true' && !contains(needs.*.result, 'failure')"
)

#: Secrets any job may name: the CI's own, not a publish credential.
SECRET_ALLOWLIST: Final = (
    re.compile(r"^GITHUB_TOKEN$"),
    re.compile(r"^HPC_CI_SSH_USER$"),
    re.compile(r"^ECCR_PULL_ROBOT_[A-Z0-9_]+$"),
    re.compile(r"^CI_PERMISSIONS_APP_[A-Z0-9_]+$"),
)
_CACHE_NAMES: Final = re.compile(r"\b(AWS_[A-Z0-9_]*|ARTIFACT_S3_[A-Z0-9_]*|SCCACHE_[A-Z0-9_]*)")
_SECRET_REF: Final = re.compile(r"secrets\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)|secrets\[\s*'([^']+)'\s*\]")
_SECRETS_TOKEN: Final = re.compile(r"\bsecrets\b")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_ECMWF_RUNNER: Final = re.compile(r"^(arc-runner-[a-z0-9-]+|hpc-submit)$")
_TRUSTED_REGISTRY: Final = "eccr.ecmwf.int/"

_ALLOWED_TRIGGERS: Final = frozenset({"push", "pull_request", "workflow_dispatch", "schedule"})


def _strip_expr(s: str) -> str:
    s = s.strip()
    if s.startswith("${{") and s.endswith("}}"):
        s = s[3:-2].strip()
    return " ".join(s.split())


def _triggers(doc: dict[Any, Any]) -> Any:
    # YAML 1.1 reads a bare `on` key as True.
    return doc.get("on", doc.get(True))


def _needs(job: dict[str, Any]) -> list[str]:
    raw = job.get("needs")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, str)]
    return []


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in job.get("steps") or [] if isinstance(s, dict)]


def _uses(step: dict[str, Any]) -> str:
    return str(step.get("uses") or "")


def _action(uses: str) -> str:
    return uses.split("@", 1)[0]


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _is_environment_job(job: dict[str, Any]) -> bool:
    return bool(job.get("environment"))


def _check_triggers(doc: dict[Any, Any]) -> list[str]:
    on = _triggers(doc)
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {t: None for t in on}
    if not isinstance(on, dict) or not on:
        return ["I1: the workflow needs explicit `on:` triggers"]
    out = []
    for name, body in on.items():
        if name not in _ALLOWED_TRIGGERS:
            out.append(
                f"I1: trigger {name!r} is not allowed (tag push, pull_request labeled, workflow_dispatch, schedule)"
            )
            continue
        body = body or {}
        if name == "push":
            if not isinstance(body, dict) or not body.get("tags") or set(body) - {"tags"}:
                out.append("I1: `push` must name `tags` and nothing else (no branches, no paths)")
        elif name == "pull_request" and (not isinstance(body, dict) or body.get("types") != ["labeled"]):
            out.append("I1: `pull_request` must be `types: [labeled]` only")
    return out


def _check_permissions(doc: dict[Any, Any]) -> list[str]:
    perms = doc.get("permissions")
    if not isinstance(perms, dict) or any(k != "contents" or v != "read" for k, v in perms.items()):
        return [f"I2: workflow permissions must be at most `contents: read`, got {perms!r}"]
    return []


def _check_concurrency(doc: dict[Any, Any]) -> list[str]:
    c = doc.get("concurrency")
    if not isinstance(c, dict) or "github.ref" not in str(c.get("group", "")):
        return ["I3: a `concurrency` group on the ref (`github.ref`) is required"]
    if c.get("cancel-in-progress") is not False:
        return ["I3: `cancel-in-progress: false` is required: a half-published release must not be cancelled"]
    return []


def _check_inherit_env_caches(doc: dict[Any, Any], jobs: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    if "env" in doc:
        out.append("I4: no workflow-level `env`: it reaches every job")
    for name, job in jobs.items():
        if job.get("secrets") == "inherit":
            out.append(f"I4: job {name!r}: no `secrets: inherit`")
    hits = sorted({m.group(1) for s in _strings(doc) for m in _CACHE_NAMES.finditer(s)})
    if hits:
        out.append(f"I4: no CI cache or artifact store in a release: {hits}")
    return out


def _secrets_of(job: dict[str, Any]) -> set[str]:
    return {m.group(1) or m.group(2) for s in _strings(job) for m in _SECRET_REF.finditer(s)}


def _check_secrets(jobs: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for name, job in jobs.items():
        if _is_environment_job(job):
            continue
        extra = sorted(s for s in _secrets_of(job) if not any(p.match(s) for p in SECRET_ALLOWLIST))
        if extra:
            out.append(f"I5: job {name!r} names secrets {extra} but has no `environment:`")
        # Anything that reads `secrets` other than by a literal name (toJSON(secrets), secrets[inputs.x])
        # could expose any secret, so it is refused outside an environment job.
        dynamic = sorted({s for s in _strings(job) if len(_SECRETS_TOKEN.findall(s)) > len(_SECRET_REF.findall(s))})
        if dynamic:
            out.append(f"I5: job {name!r} reads `secrets` without a literal name: {dynamic}")
    return out


def _check_environment_jobs(jobs: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    env_jobs = {n for n, j in jobs.items() if _is_environment_job(j)}
    after_env = _jobs_after(jobs, env_jobs)
    build_jobs = [n for n in jobs if n not in env_jobs and n not in after_env and n != RECORD_JOB]
    for name, job in jobs.items():
        for step in _steps(job):
            action = _action(_uses(step))
            if action in PUBLISH_ACTIONS and name not in env_jobs:
                out.append(f"I6: job {name!r} runs {action} without an `environment:`")
        if name not in env_jobs:
            continue
        out += _startup_env(f"environment job {name!r}", job.get("env"))
        for i, step in enumerate(_steps(job)):
            uses = _uses(step)
            if "run" in step:
                out.append(f"I6: environment job {name!r} has a `run:` step; use an action")
            elif _action(uses) not in ENVIRONMENT_ACTIONS:
                out.append(f"I6: environment job {name!r} uses {uses!r}; allowed: {list(ENVIRONMENT_ACTIONS)}")
            out += _startup_env(f"environment job {name!r} step {i + 1}", step.get("env"))
            with_ = step.get("with")
            if _action(uses) == DOWNLOAD_ARTIFACT and isinstance(with_, dict):
                if foreign := sorted(k for k in with_ if str(k) in _FOREIGN_DOWNLOAD_INPUTS):
                    out.append(
                        f"I6: environment job {name!r} step {i + 1}: download-artifact {foreign} fetches from "
                        "another run or repository; download only this run's artifacts"
                    )
        runs_on = job.get("runs-on")
        if not (isinstance(runs_on, str) and _ECMWF_RUNNER.fullmatch(runs_on)):
            out.append(f"I6: environment job {name!r} runs-on {runs_on!r}; use an arc-runner-* or hpc-submit runner")
        container = job.get("container")
        if container is not None:
            image = container.get("image") if isinstance(container, dict) else container
            creds = container.get("credentials") if isinstance(container, dict) else None
            if not (isinstance(image, str) and image.startswith(_TRUSTED_REGISTRY)) or not creds:
                out.append(
                    f"I6: environment job {name!r}: a container must come from {_TRUSTED_REGISTRY}, with credentials"
                )
        missing = sorted(set(build_jobs) - set(_needs(job)) - {name})
        if missing:
            out.append(f"I7: environment job {name!r} must need every build and test job; missing {missing}")
        if _strip_expr(str(job.get("if", ""))) != ENVIRONMENT_JOB_IF:
            out.append(f"I7: environment job {name!r} must run `if: ${{{{ {ENVIRONMENT_JOB_IF} }}}}`")
    return out


def _startup_env(where: str, env: Any) -> list[str]:
    """An environment job's `env` may not change how its processes start (LD_PRELOAD, BASH_ENV, ...)."""
    if env is None:
        return []
    if not isinstance(env, dict):
        return [f"I6: {where}: `env` must be a literal mapping, got {env!r}"]
    if bad := sorted(str(k) for k in env if _STARTUP_ENV.fullmatch(str(k))):
        return [f"I6: {where}: `env` sets {bad}, which change how processes start"]
    return []


def _jobs_after(jobs: dict[str, dict[str, Any]], roots: set[str]) -> set[str]:
    """Jobs that (transitively) need one of `roots`."""
    after: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name not in after and name not in roots and set(_needs(job)) & (roots | after):
                after.add(name)
                changed = True
    return after


def _check_run_expressions(jobs: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for name, job in jobs.items():
        for i, step in enumerate(_steps(job)):
            if "${{" in str(step.get("run", "")):
                out.append(f"I8: job {name!r} step {i + 1}: `${{{{ }}}}` inside `run:`; pass it through `env:`")
    return out


def _check_refs(jobs: dict[str, dict[str, Any]], allow_refs: Sequence[str]) -> list[str]:
    out = []
    infra_refs: set[str] = set()
    for name, job in jobs.items():
        for step in _steps(job):
            uses = _uses(step)
            if not uses or uses.startswith(("./", "docker://")):
                continue
            action, _, ref = uses.partition("@")
            if action.startswith(CI_INFRA):
                infra_refs.add(ref)
                if not _SHA_RE.fullmatch(ref) and ref not in allow_refs:
                    out.append(f"I9: job {name!r}: {uses} must be pinned to a commit SHA")
            elif not _SHA_RE.fullmatch(ref):
                out.append(f"I9: job {name!r}: {uses} must be pinned to a commit SHA")
    if len(infra_refs) > 1:
        out.append(f"I9: every ci-infrastructure action must use one ref, got {sorted(infra_refs)}")
    return out


def _check_needs(jobs: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    if GATE_JOB not in jobs or not any(_action(_uses(s)) == APPROVAL_ACTION for s in _steps(jobs[GATE_JOB])):
        out.append(f"I10: job {GATE_JOB!r} must run {APPROVAL_ACTION}")
    if RELEASE_GATE_JOB not in jobs:
        out.append(f"I10: job {RELEASE_GATE_JOB!r} (the release gate) is missing")
    for name, job in jobs.items():
        needs = set(_needs(job))
        if name != GATE_JOB and GATE_JOB not in needs:
            out.append(f"I10: job {name!r} must list {GATE_JOB!r} in `needs:` directly")
        if name not in (GATE_JOB, RELEASE_GATE_JOB) and RELEASE_GATE_JOB not in needs:
            out.append(f"I10: job {name!r} must list {RELEASE_GATE_JOB!r} in `needs:` directly")
    return out


def _check_uploads(jobs: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for name, job in jobs.items():
        for step in _steps(job):
            if _action(_uses(step)) == UPLOAD_ARTIFACT and (step.get("with") or {}).get("overwrite") not in (
                True,
                "true",
            ):
                out.append(f"job {name!r}: {UPLOAD_ARTIFACT} must set `overwrite: true` (a re-run uploads again)")
    return out


def check_text(text: str, *, allow_refs: Sequence[str] = (), name: str = "<workflow>") -> list[str]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{name}: cannot parse: {str(exc).splitlines()[0]}"]
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict) or not doc["jobs"]:
        return [f"{name}: not a workflow with jobs"]
    jobs = {str(k): (v if isinstance(v, dict) else {}) for k, v in doc["jobs"].items()}
    problems = [
        *_check_triggers(doc),
        *_check_permissions(doc),
        *_check_concurrency(doc),
        *_check_inherit_env_caches(doc, jobs),
        *_check_secrets(jobs),
        *_check_environment_jobs(jobs),
        *_check_run_expressions(jobs),
        *_check_refs(jobs, allow_refs),
        *_check_needs(jobs),
        *_check_uploads(jobs),
    ]
    return [f"{name}: {p}" for p in problems]


def check(workflow: Path, *, allow_refs: Sequence[str] = ()) -> list[str]:
    return check_text(workflow.read_text(encoding="utf-8"), allow_refs=allow_refs, name=str(workflow))


@click.command(help=__doc__)
@click.option(
    "--allow-ref",
    "allow_refs",
    multiple=True,
    help="A ci-infrastructure ref accepted besides a commit SHA, e.g. feature/cd-v3 during the experiment.",
)
@click.argument("workflows", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def main(allow_refs: tuple[str, ...], workflows: tuple[Path, ...]) -> None:
    problems = [p for w in workflows for p in check(w, allow_refs=allow_refs)]
    for p in problems:
        click.echo(p, err=True)
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
