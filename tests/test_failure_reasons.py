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
def test_the_chain_names_the_root_cause_and_links_the_jobs_in_between() -> None:
    assert failure_reasons.explain("https://github.com/o/eccodes/actions/runs/3", None).splitlines() == [
        "",
        "  caused by o/eckit › build (gcc): ctest: 2 tests failed",
        "    https://github.com/o/eckit/actions/runs/2/job/21",
        "  via https://github.com/o/eccodes/actions/runs/3/job/31",
    ]


@pytest.mark.usefixtures("api")
def test_one_failed_job_per_run_is_spelled_out_and_runner_noise_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [{"id": i, "name": f"leg {i}", "conclusion": "failure"} for i in range(7)]
    monkeypatch.setitem(_RUNS, "repos/o/x/actions/runs/9/jobs?filter=latest&per_page=100", {"jobs": jobs})
    for i in range(7):
        monkeypatch.setitem(
            _RUNS,
            f"repos/o/x/check-runs/{i}/annotations?per_page=50",
            [
                {"annotation_level": "failure", "message": "Executing the custom container implementation failed. X"},
                {"annotation_level": "failure", "message": "1 dep(s) could not be fetched."},
                {"annotation_level": "failure", "message": f"Could not fetch dep y-{i} " + "x" * 300},
            ],
        )
    lines = failure_reasons.explain_run("o/x", 9, None)
    assert lines[0].startswith("  caused by o/x › leg 0: Could not fetch dep y-0 xxx")
    assert lines[0].endswith("… (and 6 more failed job(s) of that run)")
    assert lines[1:] == ["    https://github.com/o/x/actions/runs/9/job/0"]


@pytest.mark.usefixtures("api")
def test_a_failed_job_without_errors_is_its_own_root_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [{"id": 1, "name": "leg", "conclusion": "failure"}]
    monkeypatch.setitem(_RUNS, "repos/o/y/actions/runs/8/jobs?filter=latest&per_page=100", {"jobs": jobs})
    assert failure_reasons.explain_run("o/y", 8, None) == [
        "  caused by o/y › leg: failed",
        "    https://github.com/o/y/actions/runs/8/job/1",
    ]


@pytest.mark.usefixtures("api")
def test_a_red_upstream_run_without_failed_jobs_leaves_the_linking_job_as_root_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The upstream run was re-run green since; the consumer's own job is all there is to show.
    monkeypatch.setitem(_RUNS, "repos/o/eckit/actions/runs/2/jobs?filter=latest&per_page=100", {"jobs": []})
    assert failure_reasons.explain("https://github.com/o/eccodes/actions/runs/3", None).splitlines() == [
        "",
        "  caused by o/eccodes › resolve: dep 'eckit' was red: https://github.com/o/eckit/actions/runs/2",
        "    https://github.com/o/eccodes/actions/runs/3/job/31",
    ]


@pytest.mark.usefixtures("api")
def test_no_run_url_or_an_unreadable_run_tells_nothing() -> None:
    assert failure_reasons.explain(None, None) == ""
    assert failure_reasons.explain("https://github.com/o/gone/actions/runs/1", None) == ""
