# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import pytest

from ci_infrastructure import failure_reasons

_RUNS: dict[str, Any] = {
    # eccodes' resolve failed because eckit's CI was red, which failed because of its own build.
    "repos/o/eccodes/actions/runs/3/jobs?filter=latest&per_page=100": {
        "jobs": [
            {"id": 31, "name": "resolve", "conclusion": "failure"},
            {"id": 32, "name": "collect results", "conclusion": "failure"},
            {"id": 33, "name": "pre-commit", "conclusion": "success"},
        ]
    },
    "repos/o/eccodes/check-runs/31/annotations?per_page=50": [
        {
            "annotation_level": "failure",
            "message": "dep 'eckit' was red: https://github.com/o/eckit/actions/runs/2\n  o/eckit › build: old chain",
        },
        {"annotation_level": "failure", "message": "Process completed with exit code 1."},
    ],
    "repos/o/eckit/actions/runs/2/jobs?filter=latest&per_page=100": {
        "jobs": [{"id": 21, "name": "build (gcc)", "conclusion": "failure"}]
    },
    "repos/o/eckit/check-runs/21/annotations?per_page=50": [
        {"annotation_level": "warning", "message": "slow"},
        {"annotation_level": "failure", "message": "ctest: 2 tests failed"},
        # A cycle must end.
        {"annotation_level": "failure", "message": "see https://github.com/o/eccodes/actions/runs/3"},
    ],
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(failure_reasons, "gh_api_rest", lambda path, token: _RUNS.get(path))
    failure_reasons.explain.cache_clear()


@pytest.mark.usefixtures("api")
def test_the_chain_follows_linked_runs_to_the_actual_failure() -> None:
    assert failure_reasons.explain("https://github.com/o/eccodes/actions/runs/3", None).splitlines() == [
        "",
        "  o/eccodes › resolve: dep 'eckit' was red: https://github.com/o/eckit/actions/runs/2",
        "    o/eckit › build (gcc): ctest: 2 tests failed",
        "    o/eckit › build (gcc): see https://github.com/o/eccodes/actions/runs/3",
    ]


@pytest.mark.usefixtures("api")
def test_a_failed_job_without_errors_is_still_named_and_many_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [{"id": i, "name": f"leg {i}", "conclusion": "failure"} for i in range(7)]
    monkeypatch.setitem(_RUNS, "repos/o/x/actions/runs/9/jobs?filter=latest&per_page=100", {"jobs": jobs})
    lines = failure_reasons.explain_run("o/x", 9, None)
    assert lines[0] == "  o/x › leg 0: failed"
    assert len(lines) == failure_reasons.MAX_JOBS + 1
    assert lines[-1] == "  … and 2 more failed job(s) of o/x"


@pytest.mark.usefixtures("api")
def test_no_run_url_or_an_unreadable_run_tells_nothing() -> None:
    assert failure_reasons.explain(None, None) == ""
    assert failure_reasons.explain("https://github.com/o/gone/actions/runs/1", None) == ""
