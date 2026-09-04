# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Repo-wide conventions every action under actions/ has to satisfy.

The one enforced here is the bootstrap contract documented in README.md:

    No action ever requires its caller to bootstrap. An action that uses the
    ci_infrastructure package nests ensure-infrastructure-present itself; an
    action that does not, does not.

It held by accident before this test existed -- the nesting was present in
exactly the actions that needed it -- but nothing said so and nothing checked it,
so a reader could not know, and a new action had nothing to conform to. Both
halves matter: a `needs-pkg` action without the nesting is a landmine for the
caller, and a `pure-shell` action with it builds a venv nothing uses.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTIONS_DIR = REPO_ROOT / "actions"
BOOTSTRAP = "ensure-infrastructure-present"

# Runs a stock interpreter over a script from the checkout, installing nothing --
# so it must NOT bootstrap. Exactly one action, and deliberately so: the checker
# is stdlib-only with a lower Python floor, because building a venv and pulling
# pydantic/boto3/troika would make a text comparison depend on network egress.
# See actions/check-pr-declaration/action.yml and the [project.scripts] comment in
# pyproject.toml. Listed here so dropping the exemption is a deliberate edit.
STDLIB_ONLY = {"check-pr-declaration"}


def _console_scripts() -> list[str]:
    """The `ci-infrastructure-*` entry points, read from pyproject so this cannot
    drift when one is added or renamed."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return sorted(tomllib.load(fh)["project"]["scripts"])


def _action_files() -> list[Path]:
    files = sorted(ACTIONS_DIR.glob("*/action.yml"))
    assert files, f"no actions found under {ACTIONS_DIR}"
    return files


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return doc.get("runs", {}).get("steps", []) or []


def _executable_text(doc: dict[str, Any]) -> str:
    """Everything the action actually RUNS -- step scripts and their env values.

    Deliberately not the whole file: `description:` prose mentions these names
    (announce-image and check-pr-declaration each explain why they do *not*
    bootstrap), and matching on prose would classify them backwards.
    """
    parts: list[str] = []
    for step in _steps(doc):
        parts.append(str(step.get("run", "")))
        parts.extend(str(v) for v in (step.get("env") or {}).values())
        parts.extend(str(v) for v in (step.get("with") or {}).values())
    return "\n".join(parts)


def _nests_bootstrap(doc: dict[str, Any]) -> bool:
    """By `uses:`, never by substring -- three actions name the bootstrap only in
    prose, two of them to say they deliberately avoid it."""
    return any(BOOTSTRAP in str(step.get("uses", "")) for step in _steps(doc))


def _needs_package(doc: dict[str, Any]) -> bool:
    text = _executable_text(doc)
    markers = ["CI_INFRASTRUCTURE_PYTHON", *_console_scripts()]
    return any(m in text for m in markers)


@pytest.mark.parametrize("path", _action_files(), ids=lambda p: p.parent.name)
def test_action_bootstraps_exactly_when_it_needs_the_package(path: Path) -> None:
    name = path.parent.name
    if name == BOOTSTRAP:
        pytest.skip("the bootstrap does not nest itself")

    doc = yaml.safe_load(path.read_text())
    nests = _nests_bootstrap(doc)
    needs = _needs_package(doc)

    if name in STDLIB_ONLY:
        assert not nests, (
            f"{name} is registered as stdlib-only but nests {BOOTSTRAP}; either it "
            "stopped being stdlib-only, or the exemption should go"
        )
        return

    if needs and not nests:
        pytest.fail(
            f"{name} uses the ci_infrastructure package but does not nest "
            f"{BOOTSTRAP}, so its caller has to bootstrap -- which no action may "
            "require (see the invariant in README.md)"
        )
    if nests and not needs:
        pytest.fail(
            f"{name} nests {BOOTSTRAP} but runs nothing from the package, so every caller pays for a venv it never uses"
        )


def test_the_bootstrap_is_nested_first_and_unconditionally() -> None:
    """A later bootstrap would let earlier steps run without $CI_INFRASTRUCTURE_PYTHON,
    and a conditional one would make its availability depend on the condition."""
    for path in _action_files():
        doc = yaml.safe_load(path.read_text())
        steps = _steps(doc)
        idx = [i for i, s in enumerate(steps) if BOOTSTRAP in str(s.get("uses", ""))]
        if not idx:
            continue
        assert idx == [0], f"{path.parent.name}: bootstrap must be the first step, found at {idx}"
        assert "if" not in steps[0], f"{path.parent.name}: bootstrap must not be conditional"


def test_stdlib_only_exemptions_exist_and_run_python() -> None:
    """Guards the exemption list itself: a name left behind after the action was
    deleted or renamed would silently stop enforcing anything."""
    for name in STDLIB_ONLY:
        path = ACTIONS_DIR / name / "action.yml"
        assert path.is_file(), f"{name} is exempted but has no action.yml"
        assert "python" in _executable_text(yaml.safe_load(path.read_text())).lower(), (
            f"{name} is exempted as stdlib-only python but runs no python at all"
        )


# Contexts a COMPOSITE action may reference. `matrix`, `needs`, `job`, `vars` and
# `secrets` belong to a workflow job, not to the action it calls.
_COMPOSITE_CONTEXTS: Final = frozenset({"inputs", "github", "env", "runner", "steps", "strategy"})


@pytest.mark.parametrize("path", _action_files(), ids=lambda p: p.parent.name)
def test_action_descriptions_reference_no_workflow_only_context(path: Path) -> None:
    """A `${{ ... }}` in an input description is EVALUATED, not documentation.

    GitHub template-parses the whole action file, descriptions included, and an
    expression naming a context a composite action does not have makes the action
    fail to LOAD -- every caller dies before its first step, with an error that
    points at a line of prose. build-on-hpc did exactly this by documenting its
    matrix-leg input as `${{ toJSON(matrix) }}`: "Unrecognized named-value:
    'matrix'", and every HPC leg in every repo went red at once.

    actionlint does not cover this -- the pre-commit hook only matches
    .github/workflows -- so the check lives here. Name such an expression without
    the delimiters; the caller supplies the delimited form.
    """
    text = path.read_text()
    for block in re.finditer(r"description:.*?(?=\n\s{0,4}[\w-]+:)", text, re.S):
        for expr in re.findall(r"\$\{\{(.*?)\}\}", block.group(0), re.S):
            named = set(re.findall(r"\b([a-z]+)\s*\.", expr)) | set(re.findall(r"\b([a-z]+)\s*\)", expr))
            bad = sorted(n for n in named if n not in _COMPOSITE_CONTEXTS and n not in {"toJSON", "fromJSON"})
            assert not bad, (
                f"{path}: description contains a live expression '${{{{{expr}}}}}' naming "
                f"{bad}, which a composite action cannot resolve — the action will fail to load. "
                f"Write the expression without the delimiters."
            )
