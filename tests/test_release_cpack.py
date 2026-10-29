# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The CPack initial cache of a release build."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from ci_infrastructure.manifest import ReleaseTable
from ci_infrastructure.release import ReleaseError
from ci_infrastructure.release.cpack import (
    EXCLUDE_DONE_MARKER,
    HEREDOC_DELIMITER,
    cmake_escape,
    cpack_overrides,
    exclude_script,
    rpm_filename,
)

_META = ReleaseTable.model_validate(
    {
        "summary": 'A "quoted" ${x}; list',
        "license": "Apache-2.0",
        "vendor": "ECMWF",
        "maintainer": "m@x",
        "homepage": "https://h",
    }
)


def test_cmake_escape_neutralises_quotes_references_and_lists() -> None:
    assert cmake_escape('a"b\\c${d};e') == 'a\\"b\\\\c\\${d}\\;e'
    with pytest.raises(ReleaseError, match="control character"):
        cmake_escape("a\nb")


def test_rpm_overrides() -> None:
    text = cpack_overrides(_META, generator="RPM", package_name="pkg", version="2.5.0~rc1", install_prefix="/opt/ecmwf")
    lines = text.splitlines()
    assert 'set(CPACK_PACKAGE_NAME "pkg" CACHE STRING "" FORCE)' in lines
    assert 'set(CPACK_PACKAGE_VERSION "2.5.0~rc1" CACHE STRING "" FORCE)' in lines
    assert 'set(CPACK_RPM_PACKAGE_RELEASE "1" CACHE STRING "" FORCE)' in lines
    assert 'set(CPACK_RPM_FILE_NAME "RPM-DEFAULT" CACHE STRING "" FORCE)' in lines
    assert 'set(CPACK_RPM_SPEC_MORE_DEFINE "%global __brp_check_rpaths %{nil}" CACHE STRING "" FORCE)' in lines
    assert 'set(CPACK_PACKAGING_INSTALL_PREFIX "/opt/ecmwf" CACHE STRING "" FORCE)' in lines
    # A CMake list: the separator stays, unescaped.
    assert 'set(CMAKE_INSTALL_RPATH "/opt/ecmwf/lib;/opt/ecmwf/lib64" CACHE STRING "" FORCE)' in lines
    assert 'set(CMAKE_INSTALL_RPATH_USE_LINK_PATH "OFF" CACHE BOOL "" FORCE)' in lines
    assert 'set(CPACK_PACKAGE_DESCRIPTION_SUMMARY "A \\"quoted\\" \\${x}\\; list" CACHE STRING "" FORCE)' in lines
    assert "DEBIAN" not in text


def test_deb_overrides() -> None:
    text = cpack_overrides(_META, generator="DEB", package_name="pkg", version="2.5.0~rc1", install_prefix="/opt/x")
    assert 'set(CPACK_DEBIAN_FILE_NAME "DEB-DEFAULT" CACHE STRING "" FORCE)' in text
    assert 'set(CPACK_DEBIAN_PACKAGE_SHLIBDEPS "ON" CACHE BOOL "" FORCE)' in text
    assert "CPACK_RPM" not in text


def test_unknown_generator_and_heredoc_delimiter_are_refused() -> None:
    with pytest.raises(ReleaseError, match="generator"):
        cpack_overrides(_META, generator="TGZ", package_name="p", version="1", install_prefix="/p")  # type: ignore[arg-type]
    meta = ReleaseTable.model_validate({"summary": HEREDOC_DELIMITER})
    with pytest.raises(ReleaseError, match="heredoc delimiter"):
        cpack_overrides(meta, generator="RPM", package_name="p", version="1", install_prefix="/p")


def test_rpm_filename_is_rpmbuilds_default() -> None:
    assert rpm_filename("eckit-cd-test", "2.5.0~rc1", "1", "x86_64") == "eckit-cd-test-2.5.0~rc1-1.x86_64.rpm"


def test_no_exclusion_no_script() -> None:
    assert exclude_script((), install_prefix="/opt/ecmwf") == ""


def test_exclude_script_globs_under_the_staged_prefix() -> None:
    text = exclude_script(["lib64/pkgconfig/a_test_*.pc", "share/a/t?"], install_prefix="/opt/ecmwf")
    assert 'set(_ci_root "${CPACK_TEMPORARY_DIRECTORY}/opt/ecmwf")' in text
    assert 'foreach(_ci_glob IN ITEMS "lib64/pkgconfig/a_test_*.pc" "share/a/t?")' in text
    assert "matched nothing in the install tree" in text
    assert f'file(WRITE "${{CMAKE_CURRENT_LIST_DIR}}/{EXCLUDE_DONE_MARKER}" "")' in text


_PROJECT = """\
cmake_minimum_required(VERSION 3.19)
project(p VERSION 1.0 LANGUAGES NONE)
file(WRITE "${CMAKE_BINARY_DIR}/p.pc" "p")
file(WRITE "${CMAKE_BINARY_DIR}/p_test_custom.pc" "t")
install(FILES "${CMAKE_BINARY_DIR}/p.pc" "${CMAKE_BINARY_DIR}/p_test_custom.pc" DESTINATION lib64/pkgconfig)
set(CPACK_PACKAGING_INSTALL_PREFIX /opt/ecmwf)
set(CPACK_PACKAGE_FILE_NAME p)
include(CPack)
"""


def _cpack(tmp_path: Path, globs: list[str]) -> subprocess.CompletedProcess[str]:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "CMakeLists.txt").write_text(_PROJECT)
    subprocess.run(["cmake", "-S", f"{tmp_path}/src", "-B", f"{tmp_path}/b"], check=True, capture_output=True)
    script = tmp_path / "ci-release-exclude.cmake"
    script.write_text(exclude_script(globs, install_prefix="/opt/ecmwf"))
    return subprocess.run(
        ["cpack", "-G", "TGZ", "--config", f"{tmp_path}/b/CPackConfig.cmake", "-B", f"{tmp_path}/out"]
        + ["-D", f"CPACK_PRE_BUILD_SCRIPTS={script}"],
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("cpack") is None or shutil.which("cmake") is None, reason="needs CMake")
def test_cpack_runs_the_exclude_script_on_its_staging_tree(tmp_path: Path) -> None:
    """What the release script relies on: `cpack -D CPACK_PRE_BUILD_SCRIPTS=` runs before packaging."""
    result = _cpack(tmp_path, ["lib64/pkgconfig/p_test_*.pc"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "removing /opt/ecmwf/lib64/pkgconfig/p_test_custom.pc" in result.stdout
    with tarfile.open(tmp_path / "out" / "p.tar.gz") as tar:
        files = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert files == ["p/opt/ecmwf/lib64/pkgconfig/p.pc"]
    assert (tmp_path / EXCLUDE_DONE_MARKER).is_file()


@pytest.mark.skipif(shutil.which("cpack") is None or shutil.which("cmake") is None, reason="needs CMake")
def test_cpack_fails_on_an_exclusion_that_matches_nothing(tmp_path: Path) -> None:
    result = _cpack(tmp_path, ["lib64/pkgconfig/p_test_*.pc", "lib/pkgconfig/*.pc"])
    assert result.returncode != 0
    assert "matched nothing in the install tree" in result.stdout + result.stderr
    assert not (tmp_path / "out" / "p.tar.gz").exists()
    assert not (tmp_path / EXCLUDE_DONE_MARKER).exists()
