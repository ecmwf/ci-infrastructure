# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The release record: what the gate decided, and per leg what was built, tested and published.

Reads the gate's provenance and legs and the downloaded artifacts (``release-<job_key>/build-meta.json``,
``itest-<job_key>/itest-<leg_id>.json``, ``receipt-<job_key>/receipt-<leg_id>.json``; an HPC leg's
install test ran in its build job, so its ``itest-<leg_id>.json`` is in ``release-<job_key>``); writes
``release-record.json`` and a step-summary table. download-artifact without a name puts each
artifact in its own directory, except when the run has exactly one: that one lands directly in
the download path, so a file is also looked for there (a build-meta.json only counts when it
names the leg's job key). Fails when an expected piece is missing:
the release is partial, and "Re-run failed jobs" completes it.

A leg is not built when the gate found it published (state ``already-published``), or when its
publish job found it so (receipt action ``already-published``: an HPC leg whose build job saw the
release installed under its prefix); then its receipt alone is expected.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from . import ReleaseError
from .legs import STATE_ALREADY_PUBLISHED


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _find(artifacts: Path, artifact: str, name: str) -> dict[str, Any] | None:
    """``<artifacts>/<artifact>/<name>``, or ``<artifacts>/<name>`` when the run's lone artifact landed flat."""
    nested = artifacts / artifact
    if nested.is_dir():
        return _load(nested / name)
    return _load(artifacts / name)


def _build_meta(artifacts: Path, key: str) -> dict[str, Any] | None:
    meta = _find(artifacts, f"release-{key}", "build-meta.json")
    release = meta.get("release") if meta is not None else None
    if not isinstance(release, dict) or release.get("job_key") != key:
        return None
    return meta


def _itest(artifacts: Path, key: str, lid: str) -> dict[str, Any] | None:
    """``itest-<key>/itest-<lid>.json`` (rpm: its own job), else in ``release-<key>`` (HPC: the build job's)."""
    name = f"itest-{lid}.json"
    found = _find(artifacts, f"itest-{key}", name)
    if found is None and (artifacts / f"release-{key}").is_dir():
        found = _load(artifacts / f"release-{key}" / name)
    return found


def build_record(
    *,
    provenance: Mapping[str, Any],
    legs_by_kind: Mapping[str, Any],
    artifacts: Path,
    publish: bool,
    needs: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    rows = []
    for kind, body in legs_by_kind.items():
        for leg in (body or {}).get("include") or []:
            ctx = leg.get("_release") or {}
            key, lid, state = ctx.get("job_key"), ctx.get("leg_id"), ctx.get("state")
            receipt = _find(artifacts, f"receipt-{key}", f"receipt-{lid}.json") if publish else None
            built = state != STATE_ALREADY_PUBLISHED and (receipt or {}).get("action") != STATE_ALREADY_PUBLISHED
            meta = _build_meta(artifacts, str(key)) if built else None
            itest = _itest(artifacts, str(key), str(lid)) if built else None
            if built and meta is None:
                missing.append(f"{lid}: no build (build-meta.json)")
            if built and (itest is None or itest.get("result") != "passed"):
                missing.append(f"{lid}: no passed install test")
            elif itest is not None and (itest.get("sha"), itest.get("job_key")) != (ctx.get("sha"), key):
                missing.append(
                    f"{lid}: the install test is of commit {itest.get('sha')}, job {itest.get('job_key')}; "
                    f"expected {ctx.get('sha')}, {key}"
                )
            if publish and receipt is None:
                missing.append(f"{lid}: not published (no receipt)")
            if receipt is not None and itest is not None and receipt.get("sha256") != itest.get("sha256"):
                missing.append(
                    f"{lid}: published sha256 {receipt.get('sha256')} is not the tested {itest.get('sha256')}"
                )
            rows.append(
                {
                    "kind": kind,
                    "leg_id": lid,
                    "state": state,
                    "filename": ctx.get("filename"),
                    "build": meta,
                    "itest": itest,
                    "receipt": receipt,
                }
            )
    failed = sorted(
        name for name, need in (needs or {}).items() if (need or {}).get("result") not in ("success", "skipped")
    )
    if failed:
        missing.append(f"jobs did not succeed: {failed}")
    record = {
        "status": "partial" if missing else "complete",
        "missing": missing,
        "provenance": dict(provenance),
        "legs": rows,
    }
    return record, missing


def summary(record: Mapping[str, Any]) -> str:
    prov = record.get("provenance") or {}
    lines = [
        f"### Release {prov.get('tag') or '(rehearsal)'}: {record['status']}",
        "",
        f"mode `{prov.get('mode')}`, commit `{prov.get('sha')}`, CI {(prov.get('ci_run') or {}).get('url')}",
        "",
        "| leg | state | package | sha256 | install test | published |",
        "|---|---|---|---|---|---|",
    ]
    for row in record.get("legs") or []:
        sha = ((row.get("itest") or row.get("receipt") or {}).get("sha256") or "")[:16]
        itest = (row.get("itest") or {}).get("result", "—")
        receipt = row.get("receipt") or {}
        lines.append(
            f"| {row['leg_id']} | {row['state']} | {row['filename']} | `{sha}` | {itest} | "
            f"{receipt.get('action', '—')} {receipt.get('url', '')} |"
        )
    if record.get("missing"):
        lines += ["", "**Partial — re-run failed jobs.** Missing:", *(f"- {m}" for m in record["missing"])]
    return "\n".join(lines) + "\n"


@click.command("record")
@click.option("--provenance", required=True, help="The gate's provenance output (JSON).")
@click.option("--legs", "legs_json", required=True, help="The gate's json output (JSON).")
@click.option("--publish", type=click.Choice(["true", "false"]), required=True)
@click.option("--needs", "needs_json", default="{}", help="toJSON(needs) of the record job.")
@click.option("--artifacts", type=click.Path(path_type=Path), required=True, help="The downloaded artifacts.")
@click.option("--out", type=click.Path(path_type=Path), required=True, help="Where to write release-record.json.")
def main(provenance: str, legs_json: str, publish: str, needs_json: str, artifacts: Path, out: Path) -> None:
    """Write release-record.json and the step summary; fail when the release is partial."""
    record, missing = build_record(
        provenance=json.loads(provenance),
        legs_by_kind=json.loads(legs_json),
        artifacts=artifacts,
        publish=publish == "true",
        needs=json.loads(needs_json or "{}"),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")
    text = summary(record)
    print(text)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a") as fh:
            fh.write(text)
    if missing:
        raise ReleaseError("release is partial — re-run failed jobs: " + "; ".join(missing))
