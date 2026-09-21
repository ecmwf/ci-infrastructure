# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Conventions every action under actions/ satisfies.

Bootstrap contract (docs/explanation/bootstrap.md): an action that uses the ci_infrastructure package nests
ensure-infrastructure-present itself; an action that does not, does not.
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

# Run with a bare python3 from the checkout; must not bootstrap.
STDLIB_ONLY = {"check-pr-declaration", "pick-ref"}


def _console_scripts() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return sorted(tomllib.load(fh)["project"]["scripts"])


def _action_files() -> list[Path]:
    files = sorted(ACTIONS_DIR.glob("*/action.yml"))
    assert files, f"no actions found under {ACTIONS_DIR}"
    return files


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return doc.get("runs", {}).get("steps", []) or []


def _executable_text(doc: dict[str, Any]) -> str:
    """Step scripts, env and with values; not description prose, which names these too."""
    parts: list[str] = []
    for step in _steps(doc):
        parts.append(str(step.get("run", "")))
        parts.extend(str(v) for v in (step.get("env") or {}).values())
        parts.extend(str(v) for v in (step.get("with") or {}).values())
    return "\n".join(parts)


def _nests_bootstrap(doc: dict[str, Any]) -> bool:
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
            "require (see the invariant in docs/explanation/bootstrap.md)"
        )
    if nests and not needs:
        pytest.fail(
            f"{name} nests {BOOTSTRAP} but runs nothing from the package, so every caller pays for a venv it never uses"
        )


def test_the_bootstrap_is_nested_first_and_unconditionally() -> None:
    for path in _action_files():
        doc = yaml.safe_load(path.read_text())
        steps = _steps(doc)
        idx = [i for i, s in enumerate(steps) if BOOTSTRAP in str(s.get("uses", ""))]
        if not idx:
            continue
        assert idx == [0], f"{path.parent.name}: bootstrap must be the first step, found at {idx}"
        assert "if" not in steps[0], f"{path.parent.name}: bootstrap must not be conditional"


def test_stdlib_only_exemptions_exist_and_run_python() -> None:
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
    """A `${{ }}` in a description is evaluated; a workflow-only context stops the action loading.

    actionlint only checks .github/workflows, so the check lives here.
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
