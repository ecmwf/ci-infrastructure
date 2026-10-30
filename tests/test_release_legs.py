# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Release legs and their `_release` context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from release_fixtures import HPC_LEG, RPM_LEG, write_release_repo

from ci_infrastructure.release import ReleaseError, legs
from ci_infrastructure.release.detect import ReleaseEvent
from ci_infrastructure.release.version import parse_tag, rehearsal_version

SHA = "0123456789abcdef0123456789abcdef01234567"


def _event(tag: str | None = "2.5.0-rc1") -> ReleaseEvent:
    v = parse_tag(tag) if tag else rehearsal_version("2.5.0")
    return ReleaseEvent(version=v, sha=SHA, head_sha=SHA)


def test_expand_gives_each_leg_its_release_context(tmp_path: Path) -> None:
    rm = legs.load(write_release_repo(tmp_path))
    [leg] = legs.expand(rm, ["release-rpm"], _event())["release-rpm"]
    ctx = leg["_release"]
    assert "_resolved" not in leg
    assert {k: ctx[k] for k in ("mode", "tag", "version", "pkg_version", "package_name", "prefix", "state")} == {
        "mode": "prerelease",
        "tag": "2.5.0-rc1",
        "version": "2.5.0",
        "pkg_version": "2.5.0~rc1",
        "package_name": "pkg",
        "prefix": "/opt/ecmwf",
        "state": "build",
    }
    assert ctx["filename"] == "pkg-2.5.0~rc1-1.x86_64.rpm"
    assert ctx["leg_id"] == "rpm-rocky-9-gxx-11"
    # Package, tag, leg and commit: an rc2 never meets an rc1 build's names.
    assert ctx["job_key"] == "pkg-2.5.0-rc1-rpm-rocky-9-gxx-11-01234567"
    assert 'set(CPACK_PACKAGE_VERSION "2.5.0~rc1"' in ctx["cpack_overrides"]
    assert json.loads(ctx["build_meta"])["sha"] == SHA


def test_rehearsal_job_key_and_version(tmp_path: Path) -> None:
    rm = legs.load(write_release_repo(tmp_path))
    ctx = legs.expand(rm, ["release-rpm"], _event(None))["release-rpm"][0]["_release"]
    assert (ctx["mode"], ctx["tag"], ctx["filename"]) == ("rehearsal", "", "pkg-2.5.0-1.x86_64.rpm")
    assert ctx["job_key"] == "pkg-rehearsal-rpm-rocky-9-gxx-11-01234567"


def test_package_name_comes_from_release_table(tmp_path: Path) -> None:
    manifest = write_release_repo(tmp_path)
    manifest.write_text(manifest.read_text().replace("[release]\n", '[release]\npackage-name = "pkg-test"\n'))
    ctx = legs.expand(legs.load(manifest), ["release-rpm"], _event())["release-rpm"][0]["_release"]
    assert ctx["filename"] == "pkg-test-2.5.0~rc1-1.x86_64.rpm"


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (('options = "release"\n', ""), "needs 'options'"),
        (('options = "release"\n', 'options = "release"\ntests = false\n'), "runs its tests"),
        (('channel = "rpm"', 'channel = "deb"'), "builds \\['hpc-module', 'rpm'\\] only"),
        (('channel = "rpm"', 'channel = "hpc-module"'), "builds on 'hpc-atos'"),
        (("eccr.ecmwf.int/public-ci-images/rocky9", "docker.io/rocky9"), "is not from eccr.ecmwf.int/"),
        (("eccr.ecmwf.int/docker-hub-proxy", "docker.io"), "install-test-image"),
        (('install-prefix = "/opt/ecmwf"', 'install-prefix = "/usr/local/apps/pkg"'), "allow-production"),
        (('install-prefix = "/opt/ecmwf"', 'install-prefix = "opt/ecmwf"'), "absolute"),
        (('install-prefix = "/opt/ecmwf"', 'install-prefix = "/opt/../etc"'), "normalised"),
        (('install-prefix = "/opt/ecmwf"', 'install-prefix = "/opt/$HOME"'), "outside"),
        (('install-prefix = "/opt/ecmwf"', 'install-prefix = "/"'), "normalised"),
        (('arch = "x86_64"', 'arch = "x86 64"'), "arch"),
        (('job-script = "./.ci/build.sh.j2"', 'job-script = "./.ci/build.sh"'), ".j2 recipe"),
    ],
)
def test_release_leg_validation(tmp_path: Path, change: tuple[str, str], match: str) -> None:
    rm = legs.load(write_release_repo(tmp_path, leg=RPM_LEG.replace(*change)))
    with pytest.raises(ReleaseError, match=match):
        legs.expand(rm, ["release-rpm"], _event())


def test_an_rpm_leg_builds_on_a_runner(tmp_path: Path) -> None:
    manifest = write_release_repo(tmp_path)
    manifest.write_text(
        manifest.read_text().replace("publishes = false\n", 'publishes = false\nexecution = "hpc-atos"\n')
    )
    with pytest.raises(ReleaseError, match="builds on 'runner'"):
        legs.expand(legs.load(manifest), ["release-rpm"], _event())


# --- hpc-module legs ------------------------------------------------------------------------------------


def _hpc(tmp_path: Path, tag: str | None = "2.5.0-rc1", leg: str = HPC_LEG, **kw: Any) -> dict[str, Any]:
    rm = legs.load(write_release_repo(tmp_path, leg=RPM_LEG + leg))
    [out] = legs.expand(rm, ["release-rpm", "release-hpc"], _event(tag), **kw)["release-hpc"]
    return out


def test_an_hpc_leg_installs_under_the_dry_run_root(tmp_path: Path) -> None:
    leg = _hpc(tmp_path)
    ctx = leg["_release"]
    assert ctx["channel"] == "hpc-module" and ctx["cpack_generator"] == ""
    assert ctx["prefix"] == "$SCRATCH/cd-v3-dry-run/pkg/2.5.0-rc1/GNU/8.5"
    # One shell word that expands $SCRATCH and nothing else.
    assert ctx["prefix_sh"] == '"$SCRATCH"/cd-v3-dry-run/pkg/2.5.0-rc1/GNU/8.5'
    assert ctx["leg_id"] == "hpc-hpc-atos-gnu-gxx-8"
    # Package, tag, leg and commit: also the SLURM job name (ci-<job_key>), so rc2 never reattaches to rc1.
    assert ctx["job_key"] == "pkg-2.5.0-rc1-hpc-hpc-atos-gnu-gxx-8-01234567"
    assert ctx["filename"] == "pkg-2.5.0-rc1-hpc-hpc-atos-gnu-gxx-8.tar.zst"
    assert ctx["module"] == {"name": "pkg", "version": "2.5.0-rc1", "tag": ""}
    meta = json.loads(ctx["build_meta"])
    assert meta["site"] == "hpc-batch" and meta["modules"][2] == "load gcc/old"
    assert "_resolved" not in leg


def test_the_hpc_version_dir_is_the_tag_as_pushed(tmp_path: Path) -> None:
    ctx = _hpc(tmp_path, "2.5.0-RC1")["_release"]
    assert ctx["prefix"].endswith("/pkg/2.5.0-RC1/GNU/8.5")
    assert ctx["job_key"] == "pkg-2.5.0-rc1-hpc-hpc-atos-gnu-gxx-8-01234567"


def test_a_release_is_tagged_new_and_a_rehearsal_has_the_project_version(tmp_path: Path) -> None:
    assert _hpc(tmp_path, "2.5.0", allow_production=True)["_release"]["module"]["tag"] == "new"
    ctx = _hpc(tmp_path, None)["_release"]
    assert ctx["prefix"] == "$SCRATCH/cd-v3-dry-run/pkg/2.5.0/GNU/8.5"
    assert ctx["job_key"] == "pkg-rehearsal-hpc-hpc-atos-gnu-gxx-8-01234567"


@pytest.mark.parametrize("root", ["/ec/res4/scratch/me/cd", "$HOME/cd-v3", "$HPCPERM/x/y"])
def test_another_hpc_root(tmp_path: Path, root: str) -> None:
    assert _hpc(tmp_path, hpc_root=root)["_release"]["prefix"] == f"{root}/pkg/2.5.0-rc1/GNU/8.5"


@pytest.mark.parametrize(
    ("root", "match"),
    [
        ("/usr/local/apps", "allow-production"),
        ("$SCRATCH/github-ci/x", "CI work tree"),
        ("$SCRATCH", "must be"),
        ("$FOO/x", "must be"),
        ("cd-v3", "must be"),
        ("$SCRATCH/../x", "normalised"),
        ("$SCRATCH/a b", "must be"),
        ('$SCRATCH/x";rm', "must be"),
    ],
)
def test_a_bad_hpc_root_is_refused(tmp_path: Path, root: str, match: str) -> None:
    with pytest.raises(ReleaseError, match=match):
        _hpc(tmp_path, hpc_root=root)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (('install-prefix = "{root}/{package}/{version}/GNU/8.5"', 'install-prefix = "/opt/x"'), "starts with"),
        (('/{version}/GNU/8.5"', '/{version}/../8.5"'), "only /<component>"),
        (('/{version}/GNU/8.5"', '/{version}GNU"'), "only /<component>"),
        (('install-test-command = "bin/pkg-version"', 'install-test-command = "/opt/bin/x"'), "relative"),
        (('"load cmake"]', '"load cmake; rm -rf /"]'), "modules"),
        (('site = "hpc-batch"\n', ""), "needs 'site'"),
        (('options = "release"\n', ""), "needs 'options'"),
        (('container = "eccr.ecmwf.int/', 'container = "docker.io/'), "not from eccr"),
        (('channel = "hpc-module"', 'channel = "rpm"'), "builds on 'runner'"),
    ],
)
def test_hpc_leg_validation(tmp_path: Path, change: tuple[str, str], match: str) -> None:
    with pytest.raises(ReleaseError, match=match):
        _hpc(tmp_path, leg=HPC_LEG.replace(*change))


def test_two_hpc_legs_installing_one_prefix_are_refused(tmp_path: Path) -> None:
    twin = HPC_LEG.split("\n\n", 1)[1].replace('cxx-compiler = "g++-8"', 'cxx-compiler = "g++-9"')
    rm = legs.load(write_release_repo(tmp_path, leg=RPM_LEG + HPC_LEG + twin))
    with pytest.raises(ReleaseError, match="both publish the HPC prefix"):
        legs.expand(rm, ["release-hpc"], _event())


@pytest.mark.parametrize(
    ("prefix", "word"),
    [
        ("$SCRATCH/a/b", '"$SCRATCH"/a/b'),
        ("/opt/ecmwf", "/opt/ecmwf"),
        ("/opt/a+b", "/opt/a+b"),
    ],
)
def test_prefix_shell_word(prefix: str, word: str) -> None:
    assert legs.prefix_shell_word(prefix) == word


def test_only_release_kinds_expand(tmp_path: Path) -> None:
    rm = legs.load(write_release_repo(tmp_path))
    with pytest.raises(ReleaseError, match="not a release kind"):
        legs.expand(rm, ["build"], _event())
    with pytest.raises(ReleaseError, match="no \\[matrix.release-deb\\]"):
        legs.expand(rm, ["release-deb"], _event())


def test_duplicate_leg_ids_are_refused(tmp_path: Path) -> None:
    rm = legs.load(
        write_release_repo(tmp_path, leg=RPM_LEG + RPM_LEG.replace('options = "release"', 'options = "other"'))
    )
    with pytest.raises(ReleaseError, match="share the leg id"):
        legs.expand(rm, ["release-rpm"], _event())


def test_two_legs_publishing_one_file_are_refused(tmp_path: Path) -> None:
    # Different leg ids (compiler), same nexus-target and rpm filename.
    twin = RPM_LEG.replace('cxx-compiler = "g++-11"', 'cxx-compiler = "g++-12"')
    rm = legs.load(write_release_repo(tmp_path, leg=RPM_LEG + twin))
    with pytest.raises(ReleaseError, match=r"both publish pkg-2\.5\.0~rc1-1\.x86_64\.rpm to nexus-target 'rocky-9'"):
        legs.expand(rm, ["release-rpm"], _event())
    # Another nexus-target is another file.
    other = twin.replace('nexus-target = "rocky-9"', 'nexus-target = "rocky-9-other"')
    rm = legs.load(write_release_repo(tmp_path, leg=RPM_LEG + other))
    assert len(legs.expand(rm, ["release-rpm"], _event())["release-rpm"]) == 2


def test_source_deps_are_the_string_release_refs(tmp_path: Path) -> None:
    rm = legs.load(write_release_repo(tmp_path))
    assert legs.source_deps(rm) == [{"repo": "o/ecbuild", "package": "ecbuild", "ref": "3.16.0"}]


def test_a_compiled_source_dep_is_refused(tmp_path: Path) -> None:
    manifest = write_release_repo(tmp_path)
    manifest.write_text(manifest.read_text().replace("release-ref = false", 'release-ref = "1.0.0"'))
    with pytest.raises(ReleaseError, match="compiled dep"):
        legs.source_deps(legs.load(manifest))


def test_a_manifest_without_release_is_not_a_release_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / ".ci" / "manifest.toml"
    manifest.parent.mkdir()
    manifest.write_text('[package]\nname = "p"\nrepo = "o/p"\nvisibility = "private"\ncompiler-inputs = []\n')
    with pytest.raises(ReleaseError, match="no \\[release\\] table"):
        legs.load(manifest)


@pytest.mark.parametrize(("raw", "out"), [("g++-11", "gxx-11"), ("a/b c", "a_b_c"), ("x.y-z_1", "x.y-z_1")])
def test_sanitise_id(raw: str, out: str) -> None:
    assert legs.sanitise_id(raw) == out
