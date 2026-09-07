# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The job display name a matrix leg gets, computed once for both lanes.

A leg is shown to humans in two places: the job in a repo's own `ci.yml`, and the
job (and the check run it posts back) in the generated `cross-repo-trigger*.yml`.
Those were two hand-maintained spellings of one rule and drifted apart in every
repo -- ecbuild's ci.yml titled its builds `(platform, build-type)` with a
build-type that is `Release` on every leg, while its generated lane already said
`(platform)`; three repos' `build-hpc` showed no toolchain at all.

So the rule lives here and is evaluated ONCE, by resolve_deps, into
`_resolved.job-name`. Both lanes then write the same expression:

    name: build+test (${{ matrix._resolved['job-name'] }})

What is returned is the parenthesised suffix only -- `"ubuntu-24.04, clang++-18,
default"`. The caller owns the prefix, because the two lanes disagree about it on
purpose: a repo's own workflow says `build+test`, the cross-repo one says
`eccodes/build`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Slots that are not simply the leg's value. `python-version` is prefixed so a
# bare `3.11` cannot be read as some other field, and an absent/empty `options`
# spells `default` rather than trailing a blank -- both carried over from the
# expressions these replace, so no existing job or check run is renamed.
_PYTHON_FIELD = "python-version"
_OPTIONS_FIELD = "options"


def _leg_values(legs: Sequence[Mapping[str, Any]], key: str) -> set[Any]:
    """The distinct values legs give `key`. A list value (runs-on) is unhashable,
    so normalise it to a tuple before the set sees it."""
    return {tuple(v) if isinstance(v, list) else v for v in (leg.get(key) for leg in legs)}


def _compiler_fields(legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str]) -> list[str]:
    """Leg fields naming the toolchain, for the job title.

    The compiler is what a human scans a job list for, so it gets a dedicated
    slot like `python-version` and `options` do — emitted whenever the legs carry
    it, NOT only when it happens to be the field that varies. Relying on variation
    silently dropped it from three of the five HPC lanes: eccodes' two legs differ
    only by `options` and eckit's kind has a single leg, so in both the compiler is
    constant and was never picked.

    `compiler-inputs` first: those are the fields the ARTIFACT NAME carries, so the
    title matches the identity. A repo that declares none (ecbuild — its ABI class
    rides entirely on the platform slug) falls back to a templated recipe's
    `cxx`/`cc`, which is the only other place its toolchain is written down.

    Among those, the ones that VARY win: eccodes declares both a cxx and a fortran
    compiler but only ever varies the cxx one, and pinning a constant `gfortran-13`
    into every title is noise. When none varies (a single-leg kind, or legs that
    differ only by `options`) they are all shown — a constant compiler is still the
    thing a reader wants, and it is better than an empty slot.
    """
    fields = [f for f in sorted(compiler_inputs) if any(f in leg for leg in legs)]
    if not fields:
        fields = [f for f in ("cxx", "cc") if any(f in leg for leg in legs)][:1]
    varying = [f for f in fields if len(_leg_values(legs, f)) > 1]
    return varying or fields


def first_distinguishing_field(legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str] = ()) -> str | None:
    """Pick a matrix field whose values vary across legs; used in the job display name.

    Rank 0 is exactly the artifact-name fields other than `platform` — the compiler,
    build-type, python-version, options — because those are what a reader is looking
    for in a job title. `platform` is rank 1: it already appears in the name suffix,
    though a short distro string still beats anything below. **Everything else is
    rank 2**, and that is a deliberate allowlist rather than the denylist this used
    to be.

    A denylist was wrong in both directions. It let a path become the title —
    ecbuild's HPC legs were rendering as `(hpc-atos-gnu, ./.ci/hpc/build-intel.sh)`
    because `job-script` varied and nothing excluded it. And it silently reopened
    every time a manifest grew a key: a repo adding `cc` for a templated recipe
    would have found every check run renamed (`"cc" < "cxx-compiler"`), breaking any
    branch protection pinned to the old name, with no error anywhere. A leg field
    invented by a repo for its own template is not identity and must never be a
    title; with an allowlist, that is true of keys nobody has thought of yet.
    """
    if not legs:
        return None
    keys = set(legs[0].keys())
    preferred = {*compiler_inputs, "build-type", _PYTHON_FIELD, _OPTIONS_FIELD}

    def sort_key(k: str) -> tuple[int, str]:
        if k in preferred:
            return (0, k)
        if k == "platform":
            return (1, k)
        return (2, k)

    for k in sorted(keys, key=sort_key):
        if len(_leg_values(legs, k)) > 1:
            return k
    # Nothing varies (a single-leg kind, or legs that differ only in fields the
    # ranking excludes). There is no distinguisher to show, and inventing one puts
    # a constant in every job title — `eckit/build-hpc (hpc-atos-gnu, Release)`.
    return None


def display_fields(legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str] = ()) -> list[str]:
    """The leg fields that make up a kind's job titles, left to right.

    General to detailed: the platform first (which lane and which ABI this is),
    then the toolchain, then the distinguishing leg field, then a python slot
    whenever the legs carry a python-version (so a job that varies python shows it
    even when another field is the primary distinguisher), then the build options.
    So: "build+test (ubuntu-24.04, clang++-18, py3.11)".

    Every leg of a kind gets the same fields, so two legs that differ only in a
    field nobody displays still render one title -- which is why
    `_check_leg_identity_uniqueness` refuses that shape at generate time.
    """
    compilers = _compiler_fields(legs, compiler_inputs)
    fields = ["platform", *compilers]
    # Only when it adds something: a field already given its own slot below would
    # be duplicated, and one that does not vary is a constant in every title.
    distinguishing = first_distinguishing_field(legs, compiler_inputs)
    if distinguishing is not None and distinguishing not in {"platform", _PYTHON_FIELD, _OPTIONS_FIELD, *compilers}:
        fields.append(distinguishing)
    if any(_PYTHON_FIELD in leg for leg in legs):
        fields.append(_PYTHON_FIELD)
    # `options` is the one leg field that is routinely present on some legs and
    # absent from others, and it is part of artifact identity — so two legs can
    # differ ONLY by it and otherwise render an identical name. eccodes' plain
    # and eckit-geo legs are exactly that: same compiler, same platform, two
    # different artifacts. Without this slot both the Actions-tab job and the
    # check run posted back to the dispatcher's commit are indistinguishable.
    if any(_OPTIONS_FIELD in leg for leg in legs):
        fields.append(_OPTIONS_FIELD)
    return fields


def _render(value: Any) -> str:
    """One slot's text, as GitHub would have interpolated it.

    These strings used to be produced by `${{ matrix.<field> }}`, so the two must
    agree exactly or the lane a job is named by starts to matter. An absent field
    is the empty string rather than a KeyError, and a bool is `true`/`false`, not
    Python's `True`/`False`.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def name_suffix(leg: Mapping[str, Any], legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str] = ()) -> str:
    """The parenthesised part of one leg's job title, e.g. `ubuntu-24.04, g++-13`."""
    slots: list[str] = []
    for field in display_fields(legs, compiler_inputs):
        value = _render(leg.get(field))
        if field == _PYTHON_FIELD:
            slots.append(f"py{value}")
        elif field == _OPTIONS_FIELD:
            slots.append(value or "default")
        else:
            slots.append(value)
    return ", ".join(slots)
