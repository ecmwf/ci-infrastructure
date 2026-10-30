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


def _on(cluster: str) -> Any:
    """A cluster detector: the connection runs on ``cluster``."""
    return lambda _conn: cluster


def _run(tmp_path: Path, legs: str, *, cluster_site: str = "ac-batch", on: str = "ac", **kw: Any) -> list[str]:
    return sync_tag.run_sync_tag(
        LocalConn(),
        legs_json=legs,
        kind="release-hpc",
        cluster_site=cluster_site,
        lock_root=str(tmp_path / "locks"),
        detect=_on(on),
        **kw,
    )


def _plan(site: str) -> sync_tag.Plan:
    return sync_tag.plan_for(site, sync_tag.load_clusters())


def test_a_prerelease_is_synced_not_tagged_and_nothing_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    cmds = _run(tmp_path, _gate_json(leg))
    assert cmds == [
        "module load modulemgr",
        "modulemgr -v -f sync pkg",
        "/home/deploy/software-sync/bin/software-sync -s local -t aa,ab,ad -p pkg",
        "modulemgr -v -f -m aa,ab,ad sync pkg",
    ]
    out = capsys.readouterr().out
    assert "sync-tag: on cluster ac (cluster site ac-batch)" in out
    assert "sync-tag (dry run, not run): pkg/2.5.0-rc1" in out
    assert not (tmp_path / "locks" / "modulemgr").exists()


def test_a_release_is_tagged_new(tmp_path: Path) -> None:
    module = sync_tag.Module(name="pkg", version="2.5.0", tag="new")
    cmds = sync_tag.commands(module, _plan("ab-batch"))
    assert cmds[-1] == "modulemgr -m aa,ab,ac,ad -f -v tag pkg 2.5.0 new"
    assert "-t aa,ac,ad" in cmds[2]


def test_a_site_without_targets_only_syncs_locally() -> None:
    clusters = {"site_cluster": {"x-batch": "ab"}, "sync_clusters": {"x-batch": []}, "tag_clusters": {"x-batch": []}}
    module = sync_tag.Module(name="pkg", version="2.5.0", tag="new")
    assert sync_tag.commands(module, sync_tag.plan_for("x-batch", clusters)) == [
        "module load modulemgr",
        "modulemgr -v -f sync pkg",
    ]


@pytest.mark.parametrize("site", ["hpc-batch", "lumi", "aa"])
def test_a_site_the_cluster_map_does_not_list_is_refused_not_guessed(tmp_path: Path, site: str) -> None:
    """v2 treated such a site as aa-batch: rc5 planned `-m ab,ac,ad` for a module built on ac."""
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    with pytest.raises(CIError, match=rf"cluster site '{site}' is not in module-clusters.yml"):
        _run(tmp_path, _gate_json(leg), cluster_site=site)
    assert not (tmp_path / "locks").exists()  # refused before the cluster is touched


def test_a_cluster_site_on_ag_is_refused(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    with pytest.raises(CIError, match="'ag-batch' is on ag: nothing is synced or tagged from it"):
        _run(tmp_path, _gate_json(leg), cluster_site="ag-batch", on="ag")
    assert not (tmp_path / "locks").exists()


@pytest.mark.parametrize(
    ("sync", "tag", "match"),
    [
        (["ab", "ag"], [], r"sync targets of 'aa-batch' \['ab', 'ag'\] name \['ag'\]"),
        ([], ["aa", "ag"], r"tag targets of 'aa-batch' \['aa', 'ag'\] name \['ag'\]"),
    ],
)
def test_a_plan_targeting_ag_is_refused(sync: list[str], tag: list[str], match: str) -> None:
    clusters = {
        "site_cluster": {"aa-batch": "aa"},
        "sync_clusters": {"aa-batch": sync},
        "tag_clusters": {"aa-batch": tag},
    }
    with pytest.raises(CIError, match=match):
        sync_tag.plan_for("aa-batch", clusters)


def test_the_packaged_map_never_reaches_ag() -> None:
    clusters = sync_tag.load_clusters()
    for site in clusters["site_cluster"]:
        if clusters["site_cluster"][site] in sync_tag.FORBIDDEN_CLUSTERS:
            with pytest.raises(CIError, match="nothing is synced or tagged from it"):
                sync_tag.plan_for(site, clusters)
            continue
        plan = sync_tag.plan_for(site, clusters)
        assert "ag" not in plan.sync + plan.tag_on + [plan.source]


def test_a_connection_on_ag_is_refused_under_the_lock(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    with pytest.raises(CIError, match="this connection runs on ag: nothing is synced or tagged from it"):
        _run(tmp_path, _gate_json(leg), on="ag")
    assert not (tmp_path / "locks" / "modulemgr").exists()  # released


def test_a_connection_on_another_cluster_is_refused(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    with pytest.raises(CIError, match="runs on ab, but cluster site 'ac-batch' is ac"):
        _run(tmp_path, _gate_json(leg), on="ab")


def test_the_cluster_is_read_under_the_lock(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    held: list[bool] = []

    def detect(_conn: Any) -> str:
        held.append((tmp_path / "locks" / "modulemgr").is_dir())
        return "ac"

    sync_tag.run_sync_tag(
        LocalConn(),
        legs_json=_gate_json(leg),
        kind="release-hpc",
        cluster_site="ac-batch",
        lock_root=str(tmp_path / "locks"),
        detect=detect,
    )
    assert held == [True]


@pytest.mark.parametrize(
    ("hostname", "ec_cluster", "cluster"),
    [("ac6-115.bullx", "", "ac"), ("aa1-100", "aa", "aa"), ("ag3-12.bullx", "", "ag")],
)
def test_the_cluster_of_a_hostname(hostname: str, ec_cluster: str, cluster: str) -> None:
    assert sync_tag.cluster_of_host(hostname, ec_cluster) == cluster


@pytest.mark.parametrize(
    ("hostname", "ec_cluster", "match"),
    [
        ("hpc-login", "", "cannot tell the cluster"),
        ("uan01", "", "cannot tell the cluster"),
        ("", "", "cannot tell the cluster"),
        ("ac6-115.bullx", "ab", "EC_CLUSTER says 'ab'"),
    ],
)
def test_an_unknown_or_ambiguous_host_is_refused(hostname: str, ec_cluster: str, match: str) -> None:
    with pytest.raises(CIError, match=match):
        sync_tag.cluster_of_host(hostname, ec_cluster)


class _HostConn:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout, self.returncode = stdout, returncode
        self.argv: list[str] = []

    def execute(self, command: Any, stdout: Any = None, stderr: Any = None, dryrun: bool = False) -> Any:
        self.argv = command
        conn = self

        class _Proc:
            returncode = conn.returncode

            def communicate(self) -> tuple[bytes, bytes]:
                return conn.stdout, b""

        return _Proc()


def test_detect_cluster_reads_hostname_and_ec_cluster() -> None:
    conn = _HostConn(b"ad2-7.bullx\nad\n")
    assert sync_tag.detect_cluster(conn) == "ad"
    assert conn.argv[:2] == ["bash", "-lc"]
    assert sync_tag.detect_cluster(_HostConn(b"ad2-7.bullx\n\n")) == "ad"
    with pytest.raises(CIError, match="hostname failed"):
        sync_tag.detect_cluster(_HostConn(b"", returncode=255))


def test_the_real_detector_refuses_a_machine_that_is_no_atos_node(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    _promoted(leg)
    with pytest.raises(CIError, match="cannot tell the cluster|EC_CLUSTER"):
        sync_tag.run_sync_tag(
            LocalConn(),
            legs_json=_gate_json(leg),
            kind="release-hpc",
            cluster_site="ac-batch",
            lock_root=str(tmp_path / "locks"),
        )


def test_the_cli_refuses_an_unlisted_site_before_connecting(tmp_path: Path) -> None:
    leg = hpc_leg(tmp_path)
    result = CliRunner().invoke(orchestrate.main, ["sync-tag", "--legs", _gate_json(leg)])
    assert result.exit_code != 0
    assert "cluster site 'hpc-batch' is not in module-clusters.yml" in result.output


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
        cluster_site="ac-batch",
        lock_root=str(tmp_path / "locks"),
        run_id="1-1",
        hold_seconds=5,
        sleep=sleep,
        detect=_on("ac"),
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
