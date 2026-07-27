"""Tests for the wait_for_artifacts CLI.

The CLI is a thin orchestrator over the already-tested primitives
(`resolve_ref`, `poll_for_artifact`), so these monkeypatch those out and just
assert the loop/exit-code wiring: it resolves the ref once, polls every name,
exits 0 only when all are present, and exits 1 (listing the missing ones) when
any never appear.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from click.testing import CliRunner, Result

from ci_infrastructure import wait_for_artifacts


def _run(
    monkeypatch: pytest.MonkeyPatch, args: Sequence[str], poll_result: Mapping[str, bool]
) -> tuple[list[str], Result]:
    """Run main() with patched primitives; return (names polled, CLI result).

    `poll_result` maps an artifact name -> bool returned by poll_for_artifact.
    """
    polled: list[str] = []

    monkeypatch.setattr(wait_for_artifacts, "select_token", lambda: "tok")
    monkeypatch.setattr(wait_for_artifacts, "resolve_ref", lambda repo, ref, token: "a" * 40)

    def fake_poll(repo: str, sha: str, name: str, token: str | None) -> bool:
        polled.append(name)
        return poll_result[name]

    monkeypatch.setattr(wait_for_artifacts, "poll_for_artifact", fake_poll)
    result = CliRunner().invoke(wait_for_artifacts.main, list(args))
    return polled, result


def test_all_present_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    polled, result = _run(
        monkeypatch,
        ["--repo", "o/r", "--ref", "main", "--artifact-names", "art-one art-two"],
        {"art-one": True, "art-two": True},
    )
    assert result.exit_code == 0
    assert polled == ["art-one", "art-two"]


def test_any_missing_exits_one_and_lists_them(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _run(
        monkeypatch,
        ["--repo", "o/r", "--ref", "main", "--artifact-names", "art-one art-two"],
        {"art-one": True, "art-two": False},
    )
    assert result.exit_code == 1
    err = result.stderr
    assert "art-two" in err
    assert "art-one" not in err.split("never appeared", 1)[-1]


def test_empty_names_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wait_for_artifacts, "select_token", lambda: None)
    monkeypatch.setattr(wait_for_artifacts, "resolve_ref", lambda repo, ref, token: "a" * 40)
    result = CliRunner().invoke(wait_for_artifacts.main, ["--repo", "o/r", "--ref", "main", "--artifact-names", "  "])
    assert result.exit_code == 1
