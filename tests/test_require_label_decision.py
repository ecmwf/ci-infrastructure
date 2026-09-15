# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The label-decision gate's shell script, run for real against synthetic payloads.

Same approach as test_require_ci_approval.py: the composite step's `run:` body is
executed with a stub `gh` first on PATH, so the status is asserted exactly as the
action posts it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION = REPO_ROOT / "actions" / "require-label-decision" / "action.yml"

LABEL: Final = "run-expensive-tests"
OPT_OUT: Final = "expensive-tests-not-needed"
CONTEXT: Final = "expensive-tests-label"


def _script() -> str:
    """The composite step's `run:` body, so the test cannot drift from the action."""
    doc: dict[str, Any] = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    (step,) = doc["runs"]["steps"]
    body: str = step["run"]
    return body


def _payload(*, author: str = "someone", labels: list[str] | None = None) -> dict[str, Any]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "head": {"sha": "abc123"},
            "user": {"login": author, "type": "User"},
            "labels": [{"name": n} for n in labels or []],
        },
    }


class Result:
    def __init__(self, proc: subprocess.CompletedProcess[str], gh_log: Path, outputs: Path) -> None:
        self.code = proc.returncode
        self.err = proc.stderr
        # One gh invocation per block, one argument per line.
        raw = gh_log.read_text() if gh_log.exists() else ""
        self.gh_calls = [block.splitlines() for block in raw.split("---\n") if block]
        self.outputs = outputs.read_text() if outputs.exists() else ""

    @property
    def decision(self) -> str | None:
        for line in self.outputs.splitlines():
            if line.startswith("decision="):
                return line.removeprefix("decision=")
        return None

    def posted(self, field: str) -> str:
        """The `-f field=...` value of the single status POST."""
        (call,) = self.gh_calls
        prefix = f"{field}="
        (value,) = (a.removeprefix(prefix) for a in call if a.startswith(prefix))
        return value


def _run(tmp_path: Path, payload: dict[str, Any], **inputs: str) -> Result:
    """Each call gets its own directory, so one test can run the gate twice."""
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh_log = tmp_path / "gh-calls.log"
    (bindir / "gh").write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> {gh_log}\necho --- >> {gh_log}\n'
    )
    (bindir / "gh").chmod(0o755)

    outputs = tmp_path / "outputs.txt"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_OUTPUT": str(outputs),
        "GITHUB_REPOSITORY": "ecmwf/anemoi-core",
        "RUN_URL": "https://example.invalid/run",
        "LABEL_INPUT": LABEL,
        "OPT_OUT_INPUT": OPT_OUT,
        "CONTEXT_INPUT": CONTEXT,
        "DESC_PENDING_INPUT": "",
        "DESC_DECIDED_INPUT": "",
        "EXEMPT_AUTHORS_INPUT": "",
        "DESC_EXEMPT_INPUT": "",
        **inputs,
    }
    proc = subprocess.run(["bash", "-c", _script()], env=env, capture_output=True, text=True, check=False)
    return Result(proc, gh_log, outputs)


# === The decision itself =====================================================


def test_no_label_stays_pending(tmp_path: Path) -> None:
    r = _run(tmp_path, _payload())

    assert r.code == 0
    assert r.decision == "undecided"
    assert r.posted("state") == "pending"
    assert r.posted("context") == CONTEXT


def test_either_label_is_a_decision(tmp_path: Path) -> None:
    run = _run(tmp_path, _payload(labels=[LABEL]))
    skip = _run(tmp_path, _payload(labels=[OPT_OUT]))

    assert (run.decision, run.posted("state")) == ("run", "success")
    assert (skip.decision, skip.posted("state")) == ("skip", "success")


# === exempt-authors ==========================================================


def test_an_exempt_author_needs_no_decision(tmp_path: Path) -> None:
    """A release bot has nobody to apply a label; without this its pull requests
    sit pending behind a required check forever."""
    r = _run(tmp_path, _payload(author="DeployDuck"), EXEMPT_AUTHORS_INPUT="DeployDuck")

    assert r.decision == "exempt"
    assert r.posted("state") == "success"
    assert "DeployDuck" in r.posted("description")


def test_exemption_ignores_account_type(tmp_path: Path) -> None:
    """Machine users report type "User"; the list is the caller's, so it is honoured."""
    payload = _payload(author="DeployDuck")
    assert payload["pull_request"]["user"]["type"] == "User"

    assert _run(tmp_path, payload, EXEMPT_AUTHORS_INPUT="DeployDuck").decision == "exempt"


def test_a_label_still_wins_over_the_exemption(tmp_path: Path) -> None:
    """A maintainer can still ask for the lane on a bot's pull request."""
    r = _run(tmp_path, _payload(author="DeployDuck", labels=[LABEL]), EXEMPT_AUTHORS_INPUT="DeployDuck")

    assert r.decision == "run"
    assert r.posted("description") == "Decision recorded"


def test_the_list_is_comma_separated_and_trimmed(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        _payload(author="pre-commit-ci[bot]"),
        EXEMPT_AUTHORS_INPUT=" DeployDuck , pre-commit-ci[bot] ",
    )

    assert r.decision == "exempt"


def test_only_an_exact_login_matches(tmp_path: Path) -> None:
    """No prefixes, no substrings, no globs: `*` in the list is a literal login."""
    for author, listed in [
        ("DeployDuck", "Deploy"),
        ("Deploy", "DeployDuck"),
        ("deployduck", "DeployDuck"),
        ("anyone", "*"),
    ]:
        r = _run(tmp_path, _payload(author=author), EXEMPT_AUTHORS_INPUT=listed)
        assert r.decision == "undecided", (author, listed)


def test_the_default_exempts_nobody(tmp_path: Path) -> None:
    r = _run(tmp_path, _payload(author="dependabot[bot]"))

    assert r.decision == "undecided"


def test_the_exempt_description_can_be_overridden(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        _payload(author="DeployDuck"),
        EXEMPT_AUTHORS_INPUT="DeployDuck",
        DESC_EXEMPT_INPUT="Release pull request",
    )

    assert r.posted("description") == "Release pull request"
