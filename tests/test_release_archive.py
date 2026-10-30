# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Release archive safety: one hand-made tar per rule."""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from ci_infrastructure.release import ReleaseError, archive


def _tar(path: Path, *members: tarfile.TarInfo) -> Path:
    with tarfile.open(path, "w") as tar:
        for m in members:
            data = b"x" * m.size if m.isfile() else None
            tar.addfile(m, io.BytesIO(data) if data is not None else None)
    return path


def _file(name: str, mode: int = 0o644) -> tarfile.TarInfo:
    m = tarfile.TarInfo(name)
    m.size, m.mode = 1, mode
    return m


def _dir(name: str) -> tarfile.TarInfo:
    m = tarfile.TarInfo(name)
    m.type, m.mode = tarfile.DIRTYPE, 0o755
    return m


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
    m = tarfile.TarInfo(name)
    m.type, m.linkname = kind, target
    return m


def _special(name: str, kind: bytes) -> tarfile.TarInfo:
    m = tarfile.TarInfo(name)
    m.type = kind
    return m


def test_an_install_tree_passes(tmp_path: Path) -> None:
    path = _tar(
        tmp_path / "a.tar",
        _dir("."),
        _dir("./lib64"),
        _file("./lib64/libx.so.1"),
        _link("./lib64/libx.so", "libx.so.1"),
        _link("./lib64/cmake", "../share/cmake"),
        _file("./bin/x", 0o755),
        _link("./bin/y", "bin/x", tarfile.LNKTYPE),  # a hard link names a path from the root
        _link("./bin/z", "./bin/x", tarfile.LNKTYPE),
    )
    assert archive.check_tar(path) == 8


@pytest.mark.parametrize(
    ("member", "match"),
    [
        (_file("/etc/passwd"), "absolute name"),
        (_file("./lib/../../etc/x"), "'..' component"),
        (_link("./lib/evil", "/etc/shadow"), "symlink to '/etc/shadow', outside"),
        (_link("./lib/evil", "../../etc"), "symlink to '../../etc', outside"),
        (_link("./bin/h", "/etc/passwd", tarfile.LNKTYPE), "hard link"),
        (_link("./bin/h", "./bin/later", tarfile.LNKTYPE), "hard link"),
        (_special("./dev/null", tarfile.CHRTYPE), "device or FIFO"),
        (_special("./dev/sda", tarfile.BLKTYPE), "device or FIFO"),
        (_special("./fifo", tarfile.FIFOTYPE), "device or FIFO"),
        (_file("./bin/su", 0o4755), "setuid or setgid"),
        (_file("./bin/sg", 0o2755), "setuid or setgid"),
    ],
)
def test_an_unsafe_member_is_refused(tmp_path: Path, member: tarfile.TarInfo, match: str) -> None:
    path = _tar(tmp_path / "a.tar", _dir("."), _file("./bin/x"), member)
    with pytest.raises(ReleaseError, match=match):
        archive.check_tar(path)


def test_a_name_twice_is_refused(tmp_path: Path) -> None:
    path = _tar(tmp_path / "a.tar", _file("./bin/x"), _file("bin/x"))
    with pytest.raises(ReleaseError, match="twice"):
        archive.check_tar(path)


def test_every_problem_is_named(tmp_path: Path) -> None:
    path = _tar(tmp_path / "a.tar", _file("/abs"), _file("./bin/su", 0o4755))
    with pytest.raises(ReleaseError) as exc:
        archive.check_tar(path)
    assert "absolute name" in str(exc.value) and "setuid" in str(exc.value)


def test_not_a_tar(tmp_path: Path) -> None:
    (tmp_path / "a.tar").write_bytes(b"\x00" * 10)
    with pytest.raises(ReleaseError, match="not a readable tar"):
        archive.check_tar(tmp_path / "a.tar")


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
def test_check_archive_reads_the_zstd(tmp_path: Path) -> None:
    plain = _tar(tmp_path / "a.tar", _dir("."), _file("./bin/su", 0o4755))
    subprocess.run(["zstd", "-q", str(plain), "-o", str(tmp_path / "a.tar.zst")], check=True)
    with pytest.raises(ReleaseError, match="setuid"):
        archive.check_archive(tmp_path / "a.tar.zst")


def test_check_archive_without_zstd_fails_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ReleaseError, match="zstd"):
        archive.check_archive(tmp_path / "a.tar.zst")


def test_a_symlink_chain_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    """Each target stays inside lexically; through the archive's own links ``build.log`` is ``../x``."""
    path = _tar(
        tmp_path / "a.tar",
        _link("a", "."),
        _link("b", "a/.."),
        _link("build.log", "b/x"),
        _file("bin/ok"),
    )
    with pytest.raises(ReleaseError) as exc:
        archive.check_tar(path)
    assert "'b': symlink to 'a/..', outside the tree through" in str(exc.value)
    assert "'build.log': symlink to 'b/x', outside the tree through" in str(exc.value)


@pytest.mark.parametrize(
    ("links", "match"),
    [
        ((("s", "d/.."), ("d", "e"), ("e", "s")), "symlink loop"),
        ((("x", "y"), ("y", "x")), "symlink loop"),
        ((("d", "."), ("d2", "d/d/d/..")), "leaves the tree"),
    ],
)
def test_symlink_chains_that_leave_or_loop_are_refused(
    tmp_path: Path, links: tuple[tuple[str, str], ...], match: str
) -> None:
    path = _tar(tmp_path / "a.tar", *(_link(n, t) for n, t in links))
    with pytest.raises(ReleaseError, match=match):
        archive.check_tar(path)


def test_a_member_under_a_symlinked_dir_that_leaves_is_refused(tmp_path: Path) -> None:
    path = _tar(tmp_path / "a.tar", _link("up", "."), _link("up2", "up/.."), _file("up2/x"))
    with pytest.raises(ReleaseError, match="'up2/x': its directory leads outside"):
        archive.check_tar(path)


@pytest.mark.parametrize("spelling", ["d//up", "d/./up"])
def test_a_link_spelled_off_canonical_cannot_hide_from_the_resolver(tmp_path: Path, spelling: str) -> None:
    """Tar and the kernel store ``d//up`` and ``d/./up`` as ``d/up``; the resolver must see them so."""
    path = _tar(tmp_path / "a.tar", _dir("d"), _link(spelling, ".."), _link("escape", "d/up/.."))
    with pytest.raises(ReleaseError, match="outside the tree"):
        archive.check_tar(path)


def test_symlink_chains_inside_the_tree_pass(tmp_path: Path) -> None:
    path = _tar(
        tmp_path / "a.tar",
        _dir("share"),
        _dir("share/cmake"),
        _link("lib", "share"),
        _link("lib64", "lib"),
        _link("lib64/cfg", "../share/cmake"),
        _link("here", "."),
        _link("there", "here/share/.."),
    )
    assert archive.check_tar(path) == 7


@pytest.mark.parametrize("member", [_file("./.release.json"), _link(".release.json", "x"), _dir("build.log")])
def test_a_reserved_name_is_refused(tmp_path: Path, member: tarfile.TarInfo) -> None:
    path = _tar(tmp_path / "a.tar", _file("./bin/x"), member)
    assert archive.check_tar(path) == 2  # reserved only when the caller says so
    with pytest.raises(ReleaseError, match="reserved name"):
        archive.check_tar(path, reserved=(".release.json", "build.log"))


def test_a_reserved_name_below_the_root_is_fine(tmp_path: Path) -> None:
    path = _tar(tmp_path / "a.tar", _file("./share/doc/build.log"))
    assert archive.check_tar(path, reserved=(".release.json", "build.log")) == 1
