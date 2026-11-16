# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The generated `ci.yml`: one job per `[matrix.<kind>]`, between the approval gate and `ci-result`.

Jobs that run the code under test hold only the reader App's key and the S3 key; rebuilds of
missing upstream artifacts start from `rebuild-producers`, which checks nothing out.
`[package].ci-yml = "custom"` leaves the file to the repository.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from ._github_api import EXECUTION_HPC_ATOS
from .generate_downstream_ci import (
    PACKAGE_RUNNER,
    REBUILD_JOB_ID,
    SLIM_RUNNER,
    Manifest,
    SchemaError,
    Step,
    _hpc_build_step,
    _job_script_step,
    _publish_steps,
    _reader_mint_step,
    _rebuild_job,
    _render_doc,
    upstream_repos,
)
from .manifest import FROM_DEPENDENCIES_RE, VISIBILITY_PUBLIC

CI_YML: Final = "ci.yml"
APPROVAL: Final = "ci-approval"
PRE_COMMIT: Final = "pre-commit"
RESOLVE: Final = "resolve"
HPC_GATE: Final = "hpc-gate"
RESULT: Final = "ci-result"
HPC_RESULT: Final = "collect-result-hpc"
_HPC_RUNS: Final = f"needs.{HPC_GATE}.outputs.run == 'true'"
_FIXED_JOBS: Final = frozenset({APPROVAL, PRE_COMMIT, RESOLVE, HPC_GATE, RESULT, HPC_RESULT, REBUILD_JOB_ID})
_ACTIONS: Final = "ecmwf/ci-infrastructure/actions"
_DAY: Final = 24 * 60
# A private repo's long waits run on its own runners, not on billed GitHub-hosted minutes.
_PRIVATE_WAIT_RUNNER: Final = "arc-runner-normal"
_PRIVATE_WAIT_IMAGE: Final = "eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest"

# A fork's own pull_request run does nothing; ci-fork.yml runs this file for it once approved.
_NOT_A_FORKS_OWN_RUN: Final = (
    "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository"
)


def ci_kinds(m: Manifest) -> list[str]:
    return [k for k, mk in m.matrices.items() if mk.in_ci]


def ci_cron(m: Manifest, by_repo: Mapping[str, Manifest]) -> str | None:
    """`m`'s schedule; None for none. `from-dependencies+N` is N minutes after the latest of its deps'."""
    if m.ci_schedule is False or m.ci_schedule is True:
        return None
    match = FROM_DEPENDENCIES_RE.match(m.ci_schedule)
    if match is None:
        return m.ci_schedule
    repos = sorted({d.repo for d in m.deps} - {m.repo})
    if unread := [r for r in repos if r not in by_repo]:
        raise SchemaError(f"{m.path}: ci-schedule derives from the dependencies, but cannot read {', '.join(unread)}")
    bases = [(r, cron) for r in repos if (cron := ci_cron(by_repo[r], by_repo)) is not None]
    if not bases:
        raise SchemaError(
            f"{m.path}: ci-schedule = {m.ci_schedule!r}, but no dependency has a schedule to start from; "
            'set [package].ci-schedule to a cron string, e.g. "7 0 * * *", or to false'
        )
    starts: list[int] = []
    days: set[str] = set()
    for repo, cron in bases:
        minute, hour, *rest = cron.split()
        if not (minute.isdigit() and hour.isdigit()):
            raise SchemaError(f"{m.path}: cannot derive ci-schedule from {repo}'s {cron!r}; set a cron string")
        starts.append(int(hour) * 60 + int(minute))
        days.add(" ".join(rest))
    if len(days) > 1:
        raise SchemaError(f"{m.path}: the dependencies' schedules run on different days; set a cron string")
    [day] = days
    start = max(starts) + int(match.group(1))
    if start >= _DAY and day != "* * *":
        raise SchemaError(f"{m.path}: ci-schedule passes midnight on a schedule with days ({day}); set a cron string")
    start %= _DAY
    return f"{start % 60} {start // 60} {day}"


def _local_needs(m: Manifest, kind: str) -> list[str]:
    """CI kinds of the same lane that must finish first: declared, or building a package this one needs."""
    mk = m.matrices[kind]
    out = [n for n in mk.needs if "/" not in n]
    wanted = {s for p in mk.builds for s in m.packages[p].siblings if s not in mk.builds}
    for other, ok in m.matrices.items():
        if other != kind and ok.in_ci and ok.execution == mk.execution and wanted & set(ok.packages):
            out.append(other)
    return list(dict.fromkeys(out))


def _checkout(m: Manifest) -> Step:
    with_: dict[str, Any] = {"persist-credentials": False}
    if m.full_history:
        with_["fetch-depth"] = 0
    if m.submodules:
        with_["submodules"] = m.submodules
    if not m.fork_pull_requests:
        return {"uses": "actions/checkout@v6", "with": with_}
    with_["allow-unsafe-pr-checkout"] = "${{ github.event_name == 'pull_request_target' }}"
    return {"uses": f"{_ACTIONS}/checkout-under-test@main", "with": with_}


def _git_read_step(m: Manifest) -> Step:
    """A second reader token, on `git-read` alone, for the job script."""
    step = _reader_mint_step(m.git_read)
    step["id"] = "git-read"
    step["with"]["permissions"] = "contents:read"
    return step


def _matrix(kind: str) -> str:
    return f"${{{{ fromJSON(needs.{RESOLVE}.outputs.{kind}-matrix) }}}}"


def _wait_step(kind: str, matrix: str, cond: str | None = None) -> Step:
    step: Step = {"name": f"Wait for the deps of {kind}"}
    if cond:
        step["if"] = cond
    step["uses"] = f"{_ACTIONS}/wait-for-deps@main"
    step["with"] = {"matrix": matrix, "token": "${{ steps.mint.outputs.token }}"}
    return step


def _kind_job(m: Manifest, kind: str, *, repos: Sequence[str], waits: Mapping[str, str]) -> dict[str, Any]:
    mk = m.matrices[kind]
    hpc = mk.execution == EXECUTION_HPC_ATOS
    local = _local_needs(m, kind)
    needs = [APPROVAL, RESOLVE]
    if m.deps:
        needs.append(REBUILD_JOB_ID)
    if m.ci_pre_commit and not local:
        needs.append(PRE_COMMIT)
    if hpc:
        needs.append(HPC_GATE)
    needs += [*local, *([waits[kind]] if kind in waits else [])]

    steps: list[Step] = [
        {"name": "Announce image", "uses": f"{_ACTIONS}/announce-image@main"},
        _reader_mint_step(repos),
        _checkout(m),
    ]
    fetch: dict[str, Any] = {
        "deps-json": "${{ toJSON(matrix._resolved.deps) }}",
        "token": "${{ steps.mint.outputs.token }}",
    }
    if hpc:
        fetch["stage-only"] = "true"
    steps.append({"name": "Fetch resolved deps", "id": "deps", "uses": f"{_ACTIONS}/fetch-deps@main", "with": fetch})
    if hpc:
        steps.append(_hpc_build_step(mk))
        if len(mk.packages) > 1:
            steps += _publish_steps(m, kind, "install-path", found="${{ steps.build.outputs.cache-hit }}")
    else:
        if m.git_read:
            steps.append(_git_read_step(m))
        steps.append(_job_script_step(m, mk))
        if len(mk.packages) > 1:
            steps += _publish_steps(m, kind, "install-root")
        elif mk.publishes:
            steps.append(
                {
                    "name": "Publish",
                    "uses": f"{_ACTIONS}/publish-artifact@main",
                    "with": {
                        "install-path": "${{ steps.build.outputs.install-path }}",
                        "artifact-name": "${{ matrix._resolved['own-artifact-name'] }}",
                    },
                }
            )
    table: dict[str, Any] = {"resolved": "${{ toJSON(matrix._resolved) }}"}
    if hpc:
        table["own-source"] = "${{ steps.build.outputs.cache-hit == 'true' && 'artifact' || 'built' }}"
    steps.append(
        {"name": "Print dependency table", "if": "always()", "uses": f"{_ACTIONS}/print-dep-table@main", "with": table}
    )

    container: dict[str, Any] = {"image": "${{ matrix.container || '' }}"}
    if mk.container_credentials:
        container["credentials"] = {
            "username": "${{ secrets.ECCR_PULL_ROBOT_NAME }}",
            "password": "${{ secrets.ECCR_PULL_ROBOT_TOKEN }}",
        }
    # `build` runs the tests too, unless a `test` kind does.
    title = "build+test" if kind == "build" and "test" not in ci_kinds(m) else kind
    return {
        "needs": needs,
        **({"if": _HPC_RUNS} if hpc else {}),
        "name": f"{title} (${{{{ matrix._resolved['job-name'] }}}})",
        "runs-on": "${{ matrix['runs-on'] }}",
        "container": container,
        "strategy": {"fail-fast": False, "matrix": _matrix(kind)},
        "steps": steps,
    }


def render_ci_workflow(m: Manifest, by_repo: Mapping[str, Manifest]) -> str:
    """`ci.yml` for a manifest with `ci-yml = "generated"`."""
    kinds = ci_kinds(m)
    if not kinds:
        raise SchemaError(f'{m.path}: ci-yml = "generated" needs a [matrix.<kind>] with ci = true')
    if clash := sorted(_FIXED_JOBS.intersection(kinds) | {k for k in kinds if k.startswith("wait-")}):
        raise SchemaError(f"{m.path}: [matrix.{clash[0]}] would clash with a job of ci.yml; rename the kind")
    repos = upstream_repos(m, by_repo)
    has_deps = bool(m.deps)
    hpc_kinds = [k for k in kinds if m.matrices[k].execution == EXECUTION_HPC_ATOS]
    runner_kinds = [k for k in kinds if k not in hpc_kinds]
    # Kinds that wait for their upstream artifacts before taking a build runner.
    waited = [k for k in kinds if has_deps and not _local_needs(m, k)]

    approval: dict[str, Any] = {"runs-on": SLIM_RUNNER, "permissions": {"pull-requests": "write"}}
    approval_step: Step = {"uses": f"{_ACTIONS}/require-ci-approval@main"}
    if m.fork_pull_requests:
        approval["if"] = _NOT_A_FORKS_OWN_RUN
        approval["permissions"]["contents"] = "read"
        approval_step["with"] = {"check-workflows": "true"}
    approval["steps"] = [approval_step]
    jobs: dict[str, Any] = {APPROVAL: approval}

    if m.ci_pre_commit:
        jobs[PRE_COMMIT] = {
            "needs": APPROVAL,
            "runs-on": SLIM_RUNNER,
            "env": {"FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true"},
            "steps": [_checkout(m), {"uses": f"{_ACTIONS}/pre-commit@main"}],
        }

    outputs = {f"{k}-matrix": f"${{{{ toJSON(fromJSON(steps.r.outputs.json)['{k}']) }}}}" for k in kinds}
    resolve_with: dict[str, Any] = {"matrix": ",".join(kinds), "token": "${{ steps.mint.outputs.token }}"}
    if has_deps:
        outputs["dispatch-plans"] = "${{ steps.r.outputs.dispatch-plans }}"
        resolve_with["rebuild-missing"] = "true"
    resolve_steps: list[Step] = [
        _reader_mint_step(repos),
        _checkout(m),
        {"id": "r", "uses": f"{_ACTIONS}/resolve-deps@main", "with": resolve_with},
    ]
    # With rebuilds to start, the legs' fetch-deps waits instead, once rebuild-producers has started them.
    resolve_steps += [
        _wait_step(
            k, f"${{{{ toJSON(fromJSON(steps.r.outputs.json)['{k}']) }}}}", "steps.r.outputs.dispatch-plans == '[]'"
        )
        for k in waited
        if k in runner_kinds
    ]
    jobs[RESOLVE] = {"needs": APPROVAL, "runs-on": PACKAGE_RUNNER, "outputs": outputs, "steps": resolve_steps}
    if waited:
        jobs[RESOLVE]["name"] = "resolve + wait-build"
    if has_deps:
        jobs[REBUILD_JOB_ID] = _rebuild_job(repos, [APPROVAL, RESOLVE])

    waits: dict[str, str] = {}
    if hpc_kinds:
        jobs[HPC_GATE] = {
            "needs": APPROVAL,
            "runs-on": SLIM_RUNNER,
            "permissions": {"pull-requests": "read"},
            "outputs": {"run": "${{ steps.gate.outputs.run }}"},
            "steps": [{"id": "gate", "uses": f"{_ACTIONS}/hpc-gate@main"}],
        }
        for k in hpc_kinds:
            if k not in waited:
                continue
            waits[k] = f"wait-{k}"
            needs = [APPROVAL, RESOLVE, REBUILD_JOB_ID, HPC_GATE]
            wait: dict[str, Any] = {
                "needs": needs,
                "if": _HPC_RUNS,
                "runs-on": PACKAGE_RUNNER,
            }
            if m.visibility != VISIBILITY_PUBLIC:
                wait["runs-on"] = _PRIVATE_WAIT_RUNNER
                wait["container"] = {"image": _PRIVATE_WAIT_IMAGE}
            wait["steps"] = [
                _reader_mint_step(repos),
                _wait_step(k, f"${{{{ needs.{RESOLVE}.outputs.{k}-matrix }}}}"),
            ]
            jobs[waits[k]] = wait

    for k in kinds:
        jobs[k] = _kind_job(m, k, repos=repos, waits=waits)

    if hpc_kinds:
        jobs[HPC_RESULT] = {
            "name": "collect HPC results",
            "if": f"always() && needs['{HPC_GATE}'].outputs.run == 'true'",
            "needs": [APPROVAL, HPC_GATE, *waits.values(), *hpc_kinds],
            "runs-on": SLIM_RUNNER,
            "permissions": {"statuses": "write"},
            "steps": [
                {
                    "uses": f"{_ACTIONS}/ci-result@main",
                    "with": {"needs": "${{ toJSON(needs) }}", "context": "ci-result/hpc-atos"},
                }
            ],
        }
    result_needs = [APPROVAL, *([HPC_GATE] if hpc_kinds else []), *([PRE_COMMIT] if m.ci_pre_commit else [])]
    jobs[RESULT] = {
        "name": "collect results",
        "if": "always()",
        "needs": [*result_needs, RESOLVE, *runner_kinds],
        "runs-on": SLIM_RUNNER,
        "permissions": {"statuses": "write"},
        "steps": [{"uses": f"{_ACTIONS}/ci-result@main", "with": {"needs": "${{ toJSON(needs) }}"}}],
    }

    def paths() -> dict[str, list[str]]:
        return {"paths-ignore": list(m.ci_paths_ignore)} if m.ci_paths_ignore else {}

    on: dict[str, Any] = {
        "push": {"branches": ["develop", "master", "main"], **paths()},
        "pull_request": {"types": ["opened", "reopened", "synchronize"], **paths()},
        "workflow_dispatch": None,
    }
    if m.fork_pull_requests:
        on["workflow_call"] = None
    if (cron := ci_cron(m, by_repo)) is not None:
        on["schedule"] = [{"cron": cron}]

    env = {
        "ARTIFACT_POLL_INTERVAL": "${{ vars.ARTIFACT_POLL_INTERVAL || '60' }}",
        "ARTIFACT_WAIT_TIMEOUT": "${{ vars.ARTIFACT_WAIT_TIMEOUT || '1800' }}",
        **{
            k: f"${{{{ secrets.{k} }}}}"
            for k in (
                "ARTIFACT_S3_ENDPOINT",
                "ARTIFACT_S3_BUCKET",
                "SCCACHE_BUCKET",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
            )
        },
    }
    doc = {
        "name": "CI",
        "on": on,
        "concurrency": {
            "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}",
            "cancel-in-progress": True,
        },
        "permissions": {"contents": "read"},
        "env": env,
        "jobs": jobs,
    }
    return _render_doc(m, doc)
