# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""release-record: complete, or partial with what to re-run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ci_infrastructure.release import record

KEY, LEG = "pkg-2.5.0-rc1-rpm-rocky-9-gxx-11-aaaaaaaa", "rpm-rocky-9-gxx-11"
DIGEST = "d" * 64
SHA = "a" * 40


def _legs(state: str = "build") -> dict[str, Any]:
    ctx = {"job_key": KEY, "leg_id": LEG, "state": state, "filename": "pkg-2.5.0~rc1-1.x86_64.rpm", "sha": SHA}
    return {"release-rpm": {"include": [{"platform": "rocky-9", "_release": ctx}]}}


_META = {"release": {"job_key": KEY, "leg_id": LEG}, "sha256": DIGEST}


def _write(root: Path, artifact: str, name: str, data: dict[str, Any]) -> None:
    (root / artifact).mkdir(parents=True, exist_ok=True)
    (root / artifact / name).write_text(json.dumps(data))


def _artifacts(
    root: Path, *, receipt: bool = True, receipt_sha: str = DIGEST, itest_sha: str = SHA, itest_key: str = KEY
) -> Path:
    _write(root, f"release-{KEY}", "build-meta.json", _META)
    itest = {"result": "passed", "sha256": DIGEST, "sha": itest_sha, "job_key": itest_key}
    _write(root, f"itest-{KEY}", f"itest-{LEG}.json", itest)
    if receipt:
        _write(root, f"receipt-{KEY}", f"receipt-{LEG}.json", {"action": "uploaded", "sha256": receipt_sha, "url": "u"})
    return root


_PROV = {"tag": "2.5.0-rc1", "mode": "prerelease", "sha": "a" * 40, "ci_run": {"url": "ci"}}


def test_complete(tmp_path: Path) -> None:
    rec, missing = record.build_record(
        provenance=_PROV,
        legs_by_kind=_legs(),
        artifacts=_artifacts(tmp_path),
        publish=True,
        needs={"build-rpm": {"result": "success"}, "publish-nexus": {"result": "success"}},
    )
    assert (rec["status"], missing) == ("complete", [])
    assert rec["legs"][0]["receipt"]["action"] == "uploaded"
    assert "| rpm-rocky-9-gxx-11 | build |" in record.summary(rec)


def test_a_rehearsal_expects_no_receipt(tmp_path: Path) -> None:
    rec, missing = record.build_record(
        provenance=_PROV,
        legs_by_kind=_legs(),
        artifacts=_artifacts(tmp_path, receipt=False),
        publish=False,
        needs={"publish-nexus": {"result": "skipped"}},
    )
    assert missing == []


def test_an_already_published_leg_needs_only_its_receipt(tmp_path: Path) -> None:
    _write(tmp_path, f"receipt-{KEY}", f"receipt-{LEG}.json", {"action": "already-published", "sha256": DIGEST})
    rec, missing = record.build_record(
        provenance=_PROV, legs_by_kind=_legs("already-published"), artifacts=tmp_path, publish=True
    )
    assert missing == []


@pytest.mark.parametrize(
    ("kwargs", "needs", "expected"),
    [
        ({"receipt": False}, {"publish-nexus": {"result": "failure"}}, ["not published", "did not succeed"]),
        ({"receipt_sha": "e" * 64}, {}, ["is not the tested"]),
        ({"itest_sha": "c" * 40}, {}, [f"the install test is of commit {'c' * 40}"]),
        ({"itest_key": "pkg-other"}, {}, ["job pkg-other; expected"]),
    ],
)
def test_partial(tmp_path: Path, kwargs: dict[str, Any], needs: dict[str, Any], expected: list[str]) -> None:
    rec, missing = record.build_record(
        provenance=_PROV, legs_by_kind=_legs(), artifacts=_artifacts(tmp_path, **kwargs), publish=True, needs=needs
    )
    assert rec["status"] == "partial"
    for text in expected:
        assert any(text in m for m in missing), missing
    assert "Partial — re-run failed jobs" in record.summary(rec)


def test_a_lone_artifact_is_found_where_download_artifact_puts_it(tmp_path: Path) -> None:
    """Run 40902451849: the build's artifact was the run's only one, so download-artifact (no name,
    merge-multiple false) extracted it straight into the path, with no release-<job_key>/ directory."""
    (tmp_path / "pkg-2.5.0~rc1-1.x86_64.rpm").write_bytes(b"rpm")
    (tmp_path / "SHA256SUMS").write_text(f"{DIGEST}  pkg-2.5.0~rc1-1.x86_64.rpm\n")
    (tmp_path / "build-meta.json").write_text(json.dumps(_META))
    rec, missing = record.build_record(
        provenance=_PROV,
        legs_by_kind=_legs(),
        artifacts=tmp_path,
        publish=True,
        needs={"build-rpm": {"result": "success"}, "itest-rpm": {"result": "failure"}},
    )
    assert rec["legs"][0]["build"] == _META
    assert not any("no build" in m for m in missing), missing
    assert any("no passed install test" in m for m in missing), missing


def test_a_flat_build_meta_of_another_leg_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "build-meta.json").write_text(json.dumps({"release": {"job_key": "pkg-other"}, "sha256": DIGEST}))
    _, missing = record.build_record(provenance=_PROV, legs_by_kind=_legs(), artifacts=tmp_path, publish=False)
    assert f"{LEG}: no build (build-meta.json)" in missing


def test_a_malformed_build_meta_counts_as_no_build(tmp_path: Path) -> None:
    _write(tmp_path, f"release-{KEY}", "build-meta.json", {"release": "not an object"})
    _, missing = record.build_record(provenance=_PROV, legs_by_kind=_legs(), artifacts=tmp_path, publish=False)
    assert f"{LEG}: no build (build-meta.json)" in missing


def test_a_nested_artifact_wins_over_a_flat_file(tmp_path: Path) -> None:
    _artifacts(tmp_path, receipt=False)
    (tmp_path / "build-meta.json").write_text("not json")
    rec, missing = record.build_record(provenance=_PROV, legs_by_kind=_legs(), artifacts=tmp_path, publish=False)
    assert (missing, rec["legs"][0]["build"]) == ([], _META)


def test_cli_writes_the_record_and_fails_when_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "record" / "release-record.json"
    args = ["--provenance", json.dumps(_PROV), "--legs", json.dumps(_legs()), "--artifacts", str(tmp_path / "a")]
    result = CliRunner().invoke(record.main, [*args, "--publish", "true", "--out", str(out)])
    assert result.exit_code == 1 and "release is partial" in result.output
    assert json.loads(out.read_text())["status"] == "partial"
    assert "Partial" in summary.read_text()
    _artifacts(tmp_path / "a")
    result = CliRunner().invoke(record.main, [*args, "--publish", "true", "--out", str(out)])
    assert result.exit_code == 0, result.output
