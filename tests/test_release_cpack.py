# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The CPack initial cache of a release build."""

from __future__ import annotations

import pytest

from ci_infrastructure.manifest import ReleaseTable
from ci_infrastructure.release import ReleaseError
from ci_infrastructure.release.cpack import HEREDOC_DELIMITER, cmake_escape, cpack_overrides, rpm_filename

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
