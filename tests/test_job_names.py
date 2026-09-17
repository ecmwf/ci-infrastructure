# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The job-title rule shared by both lanes."""

from __future__ import annotations

from typing import Any

from ci_infrastructure.job_names import display_fields, name_suffix


def _suffixes(legs: list[dict[str, Any]], compiler_inputs: tuple[str, ...] = ()) -> list[str]:
    return [name_suffix(leg, legs, compiler_inputs) for leg in legs]


def test_python_version_shown_when_legs_vary_it() -> None:
    legs = [
        {"cxx-compiler": "clang++-18", "python-version": "3.10", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "g++-13", "python-version": "3.12", "platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs, ("cxx-compiler",)) == [
        "ubuntu-24.04, clang++-18, py3.10",
        "ubuntu-24.04, g++-13, py3.12",
    ]


def test_python_version_not_duplicated_when_sole_distinguisher() -> None:
    legs = [
        {"python-version": "3.10", "platform": "ubuntu-24.04"},
        {"python-version": "3.12", "platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs) == ["ubuntu-24.04, py3.10", "ubuntu-24.04, py3.12"]


def test_options_slot_separates_legs_that_differ_only_by_options() -> None:
    legs = [
        {"cxx-compiler": "g++-13", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "g++-13", "options": "eckit-geo", "platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs, ("cxx-compiler",)) == [
        "ubuntu-24.04, g++-13, default",
        "ubuntu-24.04, g++-13, eckit-geo",
    ]


def test_an_explicitly_empty_options_is_still_default() -> None:
    legs = [
        {"cxx-compiler": "g++-8", "options": "", "platform": "hpc-atos-gnu"},
        {"cxx-compiler": "g++-8", "options": "eckit-geo", "platform": "hpc-atos-gnu"},
    ]
    assert _suffixes(legs, ("cxx-compiler",))[0] == "hpc-atos-gnu, g++-8, default"


def test_options_slot_omitted_when_no_leg_has_them() -> None:
    """So check-run names, and required checks on them, stay put."""
    legs = [
        {"cxx-compiler": "clang++-18", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "g++-13", "platform": "ubuntu-24.04"},
    ]
    assert display_fields(legs, ("cxx-compiler",)) == ["platform", "cxx-compiler"]


def test_toolchain_keys_do_not_become_the_title() -> None:
    """Only artifact-name fields may be the title."""
    legs = [
        {"platform": "hpc-atos-gnu", "cxx-compiler": "g++-8", "cc": "gcc", "modules": ["load prgenv/gnu"]},
        {"platform": "hpc-atos-intel", "cxx-compiler": "icpx", "cc": "icx", "modules": ["load prgenv/intel-llvm"]},
    ]
    assert display_fields(legs, ("cxx-compiler",)) == ["platform", "cxx-compiler"]


def test_constant_compiler_still_appears() -> None:
    """eccodes' shape: two legs, same toolchain, differing only by `options`."""
    legs = [
        {"cxx-compiler": "g++-8", "platform": "hpc-atos-gnu"},
        {"cxx-compiler": "g++-8", "platform": "hpc-atos-gnu", "options": "eckit-geo"},
    ]
    assert display_fields(legs, ("cxx-compiler",)) == ["platform", "cxx-compiler", "options"]


def test_single_leg_kind_names_the_compiler_not_a_constant() -> None:
    legs = [{"cxx-compiler": "g++-8", "build-type": "RelWithDebInfo", "platform": "hpc-atos-gnu"}]
    assert _suffixes(legs, ("cxx-compiler",)) == ["hpc-atos-gnu, g++-8"]


def test_a_constant_second_compiler_input_is_left_out() -> None:
    legs = [
        {"cxx-compiler": "g++-13", "fortran-compiler": "gfortran-13", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "clang++-18", "fortran-compiler": "gfortran-13", "platform": "ubuntu-24.04"},
    ]
    assert display_fields(legs, ("cxx-compiler", "fortran-compiler")) == ["platform", "cxx-compiler"]


def test_without_compiler_inputs_the_templated_cxx_names_the_job() -> None:
    legs = [
        {"platform": "hpc-atos-gnu", "cc": "gcc", "cxx": "g++"},
        {"platform": "hpc-atos-intel", "cc": "icx", "cxx": "icpx"},
    ]
    assert _suffixes(legs) == ["hpc-atos-gnu, g++", "hpc-atos-intel, icpx"]


def test_a_kind_with_no_toolchain_anywhere_is_named_by_platform_alone() -> None:
    legs = [
        {"build-type": "Release", "platform": "ubuntu-24.04"},
        {"build-type": "Release", "platform": "rocky-8"},
        {"build-type": "Release", "platform": "macos-13-arm"},
    ]
    assert _suffixes(legs) == ["ubuntu-24.04", "rocky-8", "macos-13-arm"]


def test_a_missing_field_renders_empty_not_an_error() -> None:
    legs = [
        {"cxx-compiler": "g++-13", "platform": "ubuntu-24.04"},
        {"platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs, ("cxx-compiler",)) == ["ubuntu-24.04, g++-13", "ubuntu-24.04, "]


def test_a_bool_renders_as_github_spells_it() -> None:
    legs = [
        {"platform": "hpc-atos-gnu", "tests": True},
        {"platform": "hpc-atos-gnu", "tests": False},
    ]
    assert _suffixes(legs) == ["hpc-atos-gnu, true", "hpc-atos-gnu, false"]
