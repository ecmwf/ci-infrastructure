# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""require-label-decision's `run:` body, executed with a stub `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
from conftest import action_run_body, stub_gh

LABEL: Final = "run-expensive-tests"
OPT_OUT: Final = "expensive-tests-not-needed"
CONTEXT: Final = "expensive-tests-label"


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
    gh_log = tmp_path / "gh-calls.log"
    bindir = stub_gh(tmp_path, f'for a in "$@"; do printf "%s\\n" "$a"; done >> {gh_log}\necho --- >> {gh_log}')
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
    proc = subprocess.run(
        ["bash", "-c", action_run_body("require-label-decision")], env=env, capture_output=True, text=True, check=False
    )
    return Result(proc, gh_log, outputs)


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


def test_an_exempt_author_needs_no_decision(tmp_path: Path) -> None:
    r = _run(tmp_path, _payload(author="DeployDuck"), EXEMPT_AUTHORS_INPUT="DeployDuck")
    assert r.decision == "exempt"
    assert r.posted("state") == "success"
    assert "DeployDuck" in r.posted("description")


def test_exemption_ignores_account_type(tmp_path: Path) -> None:
    """Machine users report type "User"."""
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


@pytest.mark.parametrize(
    ("author", "listed"),
    [
        ("DeployDuck", "Deploy"),
        ("Deploy", "DeployDuck"),
        ("deployduck", "DeployDuck"),
        ("anyone", "*"),
        ("dependabot[bot]", ""),
    ],
    ids=["prefix", "longer", "case", "glob", "default"],
)
def test_only_an_exact_login_matches(tmp_path: Path, author: str, listed: str) -> None:
    assert _run(tmp_path, _payload(author=author), EXEMPT_AUTHORS_INPUT=listed).decision == "undecided"


def test_the_exempt_description_can_be_overridden(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        _payload(author="DeployDuck"),
        EXEMPT_AUTHORS_INPUT="DeployDuck",
        DESC_EXEMPT_INPUT="Release pull request",
    )
    assert r.posted("description") == "Release pull request"
