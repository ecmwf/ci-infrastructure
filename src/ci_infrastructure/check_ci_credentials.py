# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Fail a workflow that hands a key beyond the reader App's to a job running code under test.

  - R1: a job that runs repository code holds at most the reader App's key and the S3 key;
    an HPC leg (build-on-hpc) also its identity's image pull and HPC user.
    A job bound to a literal `environment:` is exempt: GitHub withholds that environment's secrets
    from refs its branch policy rejects. Whether the policy exists, and the secrets live there and
    not in the repository or organisation, only the GitHub API tells; the central run checks that.
    A job runs it when it checks the repository out (under pull_request_target or workflow_run
    only with an explicit `ref:`), or builds, fetches deps or runs a local action.
    Only `schedule`, and `push` to main, master or develop or of tags, never run code under test;
    workflow_dispatch and workflow_call build against a pull request's artifacts downstream.
  - R2: a reader mint names its repositories.
  - R3: a token is minted with actions/mint-token only, never actions/create-github-app-token.
  - R4: no secret in the workflow's `env:`, no toJSON(secrets), no `secrets: inherit` into a
    workflow of another owner.

Secrets are told apart by name; GITHUB_TOKEN is not counted. Exemptions go under `credentials:`
in .github/ci-approval-allowlist.yml, each with a `reason:`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Final

import yaml

from .check_ci_approval import ALLOWLIST_NAME, _inherited_triggers, _jobs_of, _load, _triggers

READER: Final = ("CI_READER_APP_CLIENT_ID", "CI_READER_APP_PRIVATE_KEY")
S3_PREFIXES: Final = ("ARTIFACT_S3_", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SCCACHE_")
# The transitional pull of an HPC identity image, and the HPC user; only beside build-on-hpc.
HPC_PREFIXES: Final = ("ECCR_PULL_", "HPC_CI_")
NOT_COUNTED: Final = ("GITHUB_TOKEN",)

OWNER: Final = "ecmwf"
TRUSTED_PUSH_BRANCHES: Final = frozenset({"main", "master", "develop"})
# Their workflow is the default branch's: a plain checkout gets the base, not the pull request.
BASE_CHECKOUT_TRIGGERS: Final = frozenset({"pull_request_target", "workflow_run"})

MINT_TOKEN: Final = ("ecmwf/ci-infrastructure/actions/mint-token@", "./actions/mint-token")
CREATE_APP_TOKEN: Final = "actions/create-github-app-token@"
CHECKOUT: Final = "actions/checkout@"
BUILD_ON_HPC: Final = ("ecmwf/ci-infrastructure/actions/build-on-hpc@", "./actions/build-on-hpc")
RUNS_CODE: Final = tuple(
    f"{prefix}{name}{suffix}"
    for name in ("checkout-under-test", "run-job-script", "fetch-deps", "build-on-hpc", "pre-commit")
    for prefix, suffix in (("ecmwf/ci-infrastructure/actions/", "@"), ("./actions/", ""))
)

_EXPRESSION: Final = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_SECRET: Final = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)|secrets\[\s*'([^']+)'\s*\]")
_ALL_SECRETS: Final = re.compile(r"toJSON\(\s*secrets\s*\)", re.IGNORECASE)


def secret_class(name: str) -> str | None:
    if name in NOT_COUNTED:
        return None
    if name in READER:
        return "reader"
    if name.startswith(S3_PREFIXES):
        return "s3"
    if name.startswith(HPC_PREFIXES):
        return "hpc"
    return "privileged"


def _secrets_in(value: Any) -> set[str]:
    return {a or b for expr in _EXPRESSION.findall(json.dumps(value)) for a, b in _SECRET.findall(expr)}


def environment(job: dict[str, Any]) -> str | None:
    """The job's environment, if named literally; an expression may evaluate to none."""
    raw = job.get("environment")
    name = raw.get("name") if isinstance(raw, dict) else raw
    return name if isinstance(name, str) and name.strip() and "${{" not in name else None


def _with(step: dict[Any, Any]) -> dict[Any, Any]:
    block = step.get("with")
    return block if isinstance(block, dict) else {}


def _uses(job: dict[str, Any], actions: tuple[str, ...]) -> bool:
    return any(isinstance(st, dict) and str(st.get("uses", "")).startswith(actions) for st in job.get("steps") or [])


def _trusted_push(on: Any) -> bool:
    """Only to main, master or develop, or only of tags."""
    push = on.get("push") if isinstance(on, dict) else None
    if not isinstance(push, dict):
        return False
    branches = push.get("branches")
    if branches is None:
        return bool(push.get("tags")) and "branches-ignore" not in push
    return isinstance(branches, list) and bool(branches) and set(map(str, branches)) <= TRUSTED_PUSH_BRANCHES


def untrusted_triggers(doc: dict[Any, Any], workflow: Path) -> set[str]:
    triggers = _triggers(doc) | set(_inherited_triggers(workflow))
    triggers.discard("schedule")
    if "push" in triggers and _trusted_push(doc.get(True, doc.get("on"))):
        triggers.discard("push")
    return triggers


def runs_code(job: dict[str, Any], triggers: set[str]) -> bool:
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        with_block = _with(step)
        if uses.startswith(RUNS_CODE) or (uses.startswith("./") and not uses.startswith("./.github/workflows/")):
            return True
        if uses.startswith(CHECKOUT) and str(with_block.get("repository", "${{ github.repository }}")) in (
            "${{ github.repository }}",
            "${{github.repository}}",
        ):
            if "ref" in with_block or not triggers <= BASE_CHECKOUT_TRIGGERS:
                return True
    return False


def _load_exemptions(workflow: Path) -> tuple[set[tuple[str, str | None]], list[str]]:
    """(workflow, job or None for all its jobs) exempted, and problems with the entries."""
    path = workflow.parent.parent / ALLOWLIST_NAME
    doc = _load(path) if path.is_file() else None
    exempt: set[tuple[str, str | None]] = set()
    problems: list[str] = []
    for entry in (doc or {}).get("credentials") or []:
        if not isinstance(entry, dict) or "workflow" not in entry:
            problems.append(f"{path}: a credentials entry needs `workflow:`")
            continue
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{path}: the credentials entry for {entry['workflow']} needs a `reason:`")
            continue
        jobs = entry.get("jobs")
        exempt |= {(str(entry["workflow"]), None)} if jobs is None else {(str(entry["workflow"]), str(j)) for j in jobs}
    return exempt, problems


def check(workflow: Path) -> list[str]:
    try:
        doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{workflow}: cannot parse: {str(exc).splitlines()[0]}"]
    if not isinstance(doc, dict):
        return []

    exempt, problems = _load_exemptions(workflow)
    if _ALL_SECRETS.search(workflow.read_text(encoding="utf-8")):
        problems.append(f"{workflow}: R4 toJSON(secrets) hands every secret to whatever reads it")
    if leaked := sorted(n for n in _secrets_in(doc.get("env")) if secret_class(n) in ("privileged", "hpc")):
        problems.append(f"{workflow}: R4 workflow-level `env:` gives every job {', '.join(leaked)}; set it per job")

    triggers = untrusted_triggers(doc, workflow)
    for name, job in sorted(_jobs_of(doc).items()):
        if (workflow.name, None) in exempt or (workflow.name, name) in exempt:
            continue
        where = f"{workflow}: job {name!r}"
        uses = str(job.get("uses", ""))
        # A callee of this owner is checked in its own repository.
        if uses and not uses.startswith(("./", f"{OWNER}/")) and job.get("secrets") == "inherit":
            problems.append(f"{where}: R4 `secrets: inherit` into {uses}, which this check cannot see")
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_uses, with_block = str(step.get("uses", "")), _with(step)
            if step_uses.startswith(CREATE_APP_TOKEN):
                problems.append(f"{where}: R3 mint with actions/mint-token, not {step_uses.split('@')[0]}")
            if step_uses.startswith(MINT_TOKEN) and with_block.get("app") == "reader":
                if not str(with_block.get("repositories", "")).strip():
                    problems.append(f"{where}: R2 a reader mint names its `repositories:`")
        if not triggers or environment(job) or not runs_code(job, triggers):
            continue
        allowed = {"reader", "s3", None} | ({"hpc"} if _uses(job, BUILD_ON_HPC) else set())
        held = sorted(n for n in _secrets_in(job) if secret_class(n) not in allowed)
        held += sorted(
            f"the {_with(step).get('app')} App"
            for step in job.get("steps") or []
            if isinstance(step, dict)
            and str(step.get("uses", "")).startswith(MINT_TOKEN)
            and _with(step).get("app") != "reader"
        )
        if held:
            problems.append(
                f"{where}: R1 runs repository code ({', '.join(sorted(triggers))}) and holds {', '.join(held)}; "
                "only the reader App's key and the S3 key may sit there. Move it to a job without a checkout, "
                f"or exempt the job under `credentials:` in .github/{ALLOWLIST_NAME}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    problems: list[str] = []
    for arg in argv if argv is not None else sys.argv[1:]:
        problems.extend(check(Path(arg)))
    for p in dict.fromkeys(problems):
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
