# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The CMake initial cache (``cmake -C``) a release build packages with.

Ported from v2's ``cd-actions/system-package`` override generator. The metadata comes from the
manifest's ``[release]`` table, never from the event payload. Rendered when the job script is,
so ``ci-infrastructure-render --release`` reproduces the script offline.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from ..manifest import ReleaseTable
from . import ReleaseError

Generator = Literal["RPM", "DEB"]
GENERATORS: Final[tuple[Generator, ...]] = ("RPM", "DEB")

#: The heredoc delimiter the template writes the cache under; the content must not contain it.
HEREDOC_DELIMITER: Final = "CI_RELEASE_CPACK_OVERRIDES"


def cmake_escape(s: str) -> str:
    """For a CMake quoted argument: no variable reference, list split or early end survives."""
    if any(ord(c) < 0x20 for c in s):
        raise ReleaseError(f"control character in a CPack value: {s!r}")
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace(";", "\\;")


def _set(var: str, value: str, kind: str = "STRING") -> str:
    return f'set({var} "{cmake_escape(value)}" CACHE {kind} "" FORCE)'


def _set_list(var: str, values: list[str]) -> str:
    return f'set({var} "{";".join(cmake_escape(v) for v in values)}" CACHE STRING "" FORCE)'


def cpack_overrides(
    meta: ReleaseTable,
    *,
    generator: Generator,
    package_name: str,
    version: str,
    install_prefix: str,
    package_release: str = "1",
) -> str:
    if generator not in GENERATORS:
        raise ReleaseError(f"CPack generator {generator!r} is not one of {GENERATORS}")
    lines = [
        "# ci-infrastructure release: CPack overrides (generated)",
        _set("CPACK_PACKAGE_NAME", package_name),
        _set("CPACK_PACKAGE_VERSION", version),
    ]
    if meta.summary:
        lines.append(_set("CPACK_PACKAGE_DESCRIPTION_SUMMARY", meta.summary))
    if meta.vendor:
        lines.append(_set("CPACK_PACKAGE_VENDOR", meta.vendor))
    if meta.maintainer:
        lines.append(_set("CPACK_PACKAGE_CONTACT", meta.maintainer))
    if meta.homepage:
        lines.append(_set("CPACK_PACKAGE_HOMEPAGE_URL", meta.homepage))
    if generator == "RPM":
        lines += [
            _set("CPACK_RPM_PACKAGE_RELEASE", package_release),
            _set("CPACK_RPM_FILE_NAME", "RPM-DEFAULT"),
            # The RUNPATHs point into the install prefix by design; the install test checks them.
            _set("CPACK_RPM_SPEC_MORE_DEFINE", "%global __brp_check_rpaths %{nil}"),
        ]
        if meta.license:
            lines.append(_set("CPACK_RPM_PACKAGE_LICENSE", meta.license))
        if meta.vendor:
            lines.append(_set("CPACK_RPM_PACKAGE_VENDOR", meta.vendor))
        if meta.homepage:
            lines.append(_set("CPACK_RPM_PACKAGE_URL", meta.homepage))
    else:
        lines += [
            _set("CPACK_DEBIAN_PACKAGE_RELEASE", package_release),
            _set("CPACK_DEBIAN_FILE_NAME", "DEB-DEFAULT"),
            _set("CPACK_DEBIAN_PACKAGE_MAINTAINER", meta.maintainer),
            _set("CPACK_DEBIAN_PACKAGE_SHLIBDEPS", "ON", "BOOL"),
        ]
    lines += [
        _set("CPACK_PACKAGING_INSTALL_PREFIX", install_prefix),
        # GNUInstallDirs picks lib64 on RPM-based distros and lib elsewhere.
        _set_list("CMAKE_INSTALL_RPATH", [f"{install_prefix}/lib", f"{install_prefix}/lib64"]),
        _set("CMAKE_INSTALL_RPATH_USE_LINK_PATH", "OFF", "BOOL"),
    ]
    text = "\n".join(lines) + "\n"
    if HEREDOC_DELIMITER in text:
        raise ReleaseError(f"CPack overrides contain the heredoc delimiter {HEREDOC_DELIMITER}")
    return text


#: The heredoc delimiter the template writes the exclusion script under.
EXCLUDE_HEREDOC_DELIMITER: Final = "CI_RELEASE_EXCLUDE"
#: What the exclusion script writes beside itself once it has run: the job checks CPack ran it
#: (CPACK_PRE_BUILD_SCRIPTS needs CMake 3.19; an older CPack would ignore it and package everything).
EXCLUDE_DONE_MARKER: Final = "ci-release-exclude.done"


def exclude_script(patterns: Sequence[str], *, install_prefix: str) -> str:
    """A ``CPACK_PRE_BUILD_SCRIPTS`` script: remove the `[release] exclude` globs from CPack's staging tree.

    CPack installs the project into ``${CPACK_TEMPORARY_DIRECTORY}<packaging prefix>`` and packages
    that tree, so what is removed here is not in the package. All globs are expanded before anything
    is removed; one that matches nothing fails the build, so a stale exclusion cannot linger.
    Empty when there is nothing to exclude.
    """
    if not patterns:
        return ""
    items = " ".join(f'"{cmake_escape(p)}"' for p in patterns)
    text = f"""# ci-infrastructure release: [release] exclude (generated), run by CPack before it packages
set(_ci_root "${{CPACK_TEMPORARY_DIRECTORY}}{cmake_escape(install_prefix)}")
if(NOT IS_DIRECTORY "${{_ci_root}}")
  message(FATAL_ERROR "ci-release: [release] exclude: no staging tree at ${{_ci_root}}")
endif()
set(_ci_hits "")
set(_ci_unmatched "")
foreach(_ci_glob IN ITEMS {items})
  file(GLOB _ci_glob_hits LIST_DIRECTORIES true RELATIVE "${{_ci_root}}" "${{_ci_root}}/${{_ci_glob}}")
  if(NOT _ci_glob_hits)
    list(APPEND _ci_unmatched "${{_ci_glob}}")
  endif()
  list(APPEND _ci_hits ${{_ci_glob_hits}})
endforeach()
if(_ci_unmatched)
  message(FATAL_ERROR "ci-release: [release] exclude: matched nothing in the install tree: ${{_ci_unmatched}}")
endif()
list(REMOVE_DUPLICATES _ci_hits)
foreach(_ci_hit IN LISTS _ci_hits)
  message(STATUS "ci-release: [release] exclude: removing {cmake_escape(install_prefix)}/${{_ci_hit}}")
  file(REMOVE_RECURSE "${{_ci_root}}/${{_ci_hit}}")
endforeach()
file(WRITE "${{CMAKE_CURRENT_LIST_DIR}}/{EXCLUDE_DONE_MARKER}" "")
"""
    if EXCLUDE_HEREDOC_DELIMITER in text:
        raise ReleaseError(f"the exclusion script contains the heredoc delimiter {EXCLUDE_HEREDOC_DELIMITER}")
    return text


def rpm_filename(package_name: str, version: str, package_release: str, arch: str) -> str:
    """What ``CPACK_RPM_FILE_NAME=RPM-DEFAULT`` names the package: rpmbuild's own scheme."""
    return f"{package_name}-{version}-{package_release}.{arch}.rpm"
