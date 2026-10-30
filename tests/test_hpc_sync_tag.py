# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""hpc-sync-tag (dry run): the cross-repo modulemgr lock, the promoted trees, the commands it would run."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from test_hpc_release_archive import LocalConn, hpc_leg

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import orchestrate, release_tree, sync_tag


def _gate_json(*legs: dict[str, Any]) -> str:
    return json.dumps({"release-rpm": {"include": []}, "release-hpc": {"include": list(legs)}})


def _promoted(leg: dict[str, Any]) -> None:
    prefix = Path(leg["_release"]["prefix"])
    prefix.mkdir(parents=True)
    (prefix / ".release.json").write_text(json.dumps({**release_tree.identity(leg["_release"]), "sha256": "d" * 64}))


def _run(tmp_path: Path, legs: str, **kw: Any) -> list[str]:
    return sync_tag.run_sync_tag(
        LocalConn(), legs_json=legs, kind="release-hpc", site="hpc-batch", lock_root=str(tmp_path / "locks"), **kw
    )


def test_a_prerelease_is_synced_not_tagged_and_nothing_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    cmds = _run(tmp_path, _gate_json(leg))
    assert cmds == [
        "module load modulemgr",
        "modulemgr -v -f sync pkg",
        "/home/deploy/software-sync/bin/software-sync -s local -t ab,ac,ad -p pkg",
        "modulemgr -v -f -m ab,ac,ad sync pkg",
    ]
    out = capsys.readouterr().out
    assert "sync-tag (dry run, not run): pkg/2.5.0-rc1" in out
    assert not (tmp_path / "locks" / "modulemgr").exists()


def test_a_release_is_tagged_new(tmp_path: Path) -> None:
    module = sync_tag.Module(name="pkg", version="2.5.0", tag="new")
    cmds = sync_tag.commands(module, site="ab-batch", clusters=sync_tag.load_clusters())
    assert cmds[-1] == "modulemgr -m aa,ab,ac,ad -f -v tag pkg 2.5.0 new"
    assert "-t aa,ac,ad" in cmds[2]


def test_a_site_without_clusters_only_syncs_locally(tmp_path: Path) -> None:
    module = sync_tag.Module(name="pkg", version="2.5.0", tag="new")
    assert sync_tag.commands(module, site="lumi", clusters=sync_tag.load_clusters()) == [
        "module load modulemgr",
        "modulemgr -v -f sync pkg",
    ]


def test_a_module_not_promoted_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CIError, match="promote it first"):
        _run(tmp_path, _gate_json(hpc_leg(tmp_path)))


def test_another_release_at_the_prefix_is_refused(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    other = {**leg, "_release": {**leg["_release"], "sha": "f" * 40}}
    _promoted(other)
    with pytest.raises(CIError, match="promote it first"):
        _run(tmp_path, _gate_json(leg))


def test_the_kill_switch_stops_sync(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / "DISABLED").write_text("maintenance\n")
    with pytest.raises(CIError, match="disabled on this HPC"):
        _run(tmp_path, _gate_json(leg))


def test_s6_a_second_run_waits_for_the_holder_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S6 in miniature: another run holds modulemgr (fresh); this one waits, names it, then times out."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    lock = tmp_path / "locks" / "modulemgr"
    lock.mkdir(parents=True)
    (lock / "owner").write_text('{"run_id":"5-1","repository":"o/other"}\n')
    with pytest.raises(CIError, match="waiting for the sync/tag lock .*o/other"):
        _run(tmp_path, _gate_json(leg), timeout=0)


def test_a_stale_modulemgr_lock_is_broken(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    lock = tmp_path / "locks" / "modulemgr"
    lock.mkdir(parents=True)
    (lock / "owner").write_text('{"run_id":"5-1"}\n')
    old = time.time() - 3 * 3600
    os.utime(lock, (old, old))
    _run(tmp_path, _gate_json(leg))
    assert "broke the stale lock" in capsys.readouterr().out


def test_hold_seconds_holds_the_lock(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    held: list[bool] = []

    def sleep(seconds: float) -> None:
        held.append((tmp_path / "locks" / "modulemgr").is_dir())

    sync_tag.sync_tag(
        LocalConn(),
        modules=sync_tag.modules_of(_gate_json(leg), "release-hpc"),
        site="hpc-batch",
        lock_root=str(tmp_path / "locks"),
        run_id="1-1",
        hold_seconds=5,
        sleep=sleep,
    )
    assert held == [True]


@pytest.mark.parametrize(
    ("module", "match"),
    [
        ({"name": "pkg;rm", "version": "1", "tag": ""}, "module name"),
        ({"name": "pkg", "version": "1 2", "tag": ""}, "module version"),
        ({"name": "pkg", "version": "1", "tag": "a/b"}, "module tag"),
    ],
)
def test_names_rendered_into_commands_are_checked(tmp_path: Path, module: dict[str, str], match: str) -> None:
    leg = hpc_leg(tmp_path)
    leg["_release"]["module"] = module
    with pytest.raises(CIError, match=match):
        sync_tag.modules_of(_gate_json(leg), "release-hpc")


def test_legs_of_one_module_are_synced_once(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    twin = {**leg, "_release": {**leg["_release"], "leg_id": "hpc-other"}}
    [module] = sync_tag.modules_of(_gate_json(leg, twin), "release-hpc")
    assert len(module.legs) == 2


def test_a_rehearsal_is_never_synced(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    leg["_release"]["mode"] = "rehearsal"
    with pytest.raises(CIError, match="never synced"):
        _run(tmp_path, _gate_json(leg))


def test_no_hpc_legs_is_nothing_to_do(tmp_path: Path) -> None:
    result = CliRunner().invoke(orchestrate.main, ["sync-tag", "--legs", '{"release-rpm": {"include": []}}'])
    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


def test_the_site_comes_from_the_legs(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    assert sync_tag.site_of(_gate_json(leg), "release-hpc") == "hpc-batch"
    other = {**leg, "site": "ab-batch"}
    with pytest.raises(CIError, match="pass --site"):
        sync_tag.site_of(_gate_json(leg, other), "release-hpc")
