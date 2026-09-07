# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the job-title rule shared by both lanes.

These used to live in test_generate_downstream_ci / test_generate_hpc as
assertions on the rendered `${{ }}` expression. The rule now produces the string
itself, in resolve_deps, so it can be checked against the leg it describes rather
than against the YAML that used to interpolate it -- which also means a case is a
list of legs and not a manifest fixture.
"""

from __future__ import annotations

from typing import Any

from ci_infrastructure.job_names import display_fields, name_suffix


def _suffixes(legs: list[dict[str, Any]], compiler_inputs: tuple[str, ...] = ()) -> list[str]:
    return [name_suffix(leg, legs, compiler_inputs) for leg in legs]


def test_python_version_shown_when_legs_vary_it() -> None:
    """A kind whose legs vary python-version shows a py<version> slot in the job
    name (and thus in the check runs reported back to the dispatcher), even when
    another field -- here cxx-compiler, which sorts first -- is the primary
    distinguisher. Otherwise legs on the same compiler/platform but different
    python are indistinguishable in the Actions UI."""
    legs = [
        {"cxx-compiler": "clang++-18", "python-version": "3.10", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "g++-13", "python-version": "3.12", "platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs, ("cxx-compiler",)) == [
        "ubuntu-24.04, clang++-18, py3.10",
        "ubuntu-24.04, g++-13, py3.12",
    ]


def test_python_version_not_duplicated_when_sole_distinguisher() -> None:
    """When python-version is itself the distinguishing field, it appears exactly
    once (py-prefixed) -- never as both the primary slot and a second py<version>
    slot."""
    legs = [
        {"python-version": "3.10", "platform": "ubuntu-24.04"},
        {"python-version": "3.12", "platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs) == ["ubuntu-24.04, py3.10", "ubuntu-24.04, py3.12"]


def test_options_slot_separates_legs_that_differ_only_by_options() -> None:
    """Two legs that differ ONLY by `options` must not render the same job name.

    `options` is part of artifact identity, so such legs publish different
    artifacts -- but compiler and platform, the fields the name is built from, are
    identical. Without an options slot both the Actions-tab job and the check run
    posted back to the dispatcher's commit are indistinguishable. The leg without
    options shows `default` rather than a blank.
    """
    legs = [
        {"cxx-compiler": "g++-13", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "g++-13", "options": "eckit-geo", "platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs, ("cxx-compiler",)) == [
        "ubuntu-24.04, g++-13, default",
        "ubuntu-24.04, g++-13, eckit-geo",
    ]


def test_an_explicitly_empty_options_is_still_default() -> None:
    """eccodes writes `options = ""` on its plain HPC leg. GitHub's `||` treated
    that as falsy and so must this, or the two lanes disagree on one leg."""
    legs = [
        {"cxx-compiler": "g++-8", "options": "", "platform": "hpc-atos-gnu"},
        {"cxx-compiler": "g++-8", "options": "eckit-geo", "platform": "hpc-atos-gnu"},
    ]
    assert _suffixes(legs, ("cxx-compiler",))[0] == "hpc-atos-gnu, g++-8, default"


def test_options_slot_omitted_when_no_leg_has_them() -> None:
    """The options slot is opt-in: a kind whose legs never set `options` keeps the
    name it had before the slot existed, so unrelated repos' check-run names -- and
    any required-status-check configured on them -- do not shift."""
    legs = [
        {"cxx-compiler": "clang++-18", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "g++-13", "platform": "ubuntu-24.04"},
    ]
    assert display_fields(legs, ("cxx-compiler",)) == ["platform", "cxx-compiler"]


def test_toolchain_keys_do_not_become_the_title() -> None:
    """`cc` sorts before `cxx-compiler`, so under the old denylist a repo adding it
    for a templated recipe would silently rename every check run and break branch
    protection pinned to the old name. Only artifact-name fields may be the title."""
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
    """eckit's shape. With nothing varying, the old code fell back to the
    first-ranked field and titled every job with a constant `Release`."""
    legs = [{"cxx-compiler": "g++-8", "build-type": "RelWithDebInfo", "platform": "hpc-atos-gnu"}]
    assert _suffixes(legs, ("cxx-compiler",)) == ["hpc-atos-gnu, g++-8"]


def test_a_constant_second_compiler_input_is_left_out() -> None:
    """eccodes' runner lane varies cxx but never fortran; pinning a constant
    `gfortran-13` into every title is noise, so only the varying one shows."""
    legs = [
        {"cxx-compiler": "g++-13", "fortran-compiler": "gfortran-13", "platform": "ubuntu-24.04"},
        {"cxx-compiler": "clang++-18", "fortran-compiler": "gfortran-13", "platform": "ubuntu-24.04"},
    ]
    assert display_fields(legs, ("cxx-compiler", "fortran-compiler")) == ["platform", "cxx-compiler"]


def test_without_compiler_inputs_the_templated_cxx_names_the_job() -> None:
    """ecbuild's HPC shape: no compiler in its artifact identity at all, so the only
    place its toolchain is written down is the leg its `.j2` recipe reads."""
    legs = [
        {"platform": "hpc-atos-gnu", "cc": "gcc", "cxx": "g++"},
        {"platform": "hpc-atos-intel", "cc": "icx", "cxx": "icpx"},
    ]
    assert _suffixes(legs) == ["hpc-atos-gnu, g++", "hpc-atos-intel, icpx"]


def test_a_kind_with_no_toolchain_anywhere_is_named_by_platform_alone() -> None:
    """ecbuild's runner shape: compiler-independent CMake macros, so no compiler
    field and no templated `cxx`. Its ci.yml used to pad the gap with `build-type`,
    which is `Release` on every leg and told a reader nothing."""
    legs = [
        {"build-type": "Release", "platform": "ubuntu-24.04"},
        {"build-type": "Release", "platform": "rocky-8"},
        {"build-type": "Release", "platform": "macos-13-arm"},
    ]
    assert _suffixes(legs) == ["ubuntu-24.04", "rocky-8", "macos-13-arm"]


def test_a_missing_field_renders_empty_not_an_error() -> None:
    """These strings were `${{ matrix.<field> }}`, where an absent field is the
    empty string. A leg missing a field the kind displays must render the same way
    rather than raising -- one ragged leg would otherwise fail the whole resolve."""
    legs = [
        {"cxx-compiler": "g++-13", "platform": "ubuntu-24.04"},
        {"platform": "ubuntu-24.04"},
    ]
    assert _suffixes(legs, ("cxx-compiler",)) == ["ubuntu-24.04, g++-13", "ubuntu-24.04, "]


def test_a_bool_renders_as_github_spells_it() -> None:
    """`tests = false` is rank 2, so it only reaches a title when nothing better
    varies -- but when it does, GitHub wrote `false`, not Python's `False`."""
    legs = [
        {"platform": "hpc-atos-gnu", "tests": True},
        {"platform": "hpc-atos-gnu", "tests": False},
    ]
    assert _suffixes(legs) == ["hpc-atos-gnu, true", "hpc-atos-gnu, false"]
