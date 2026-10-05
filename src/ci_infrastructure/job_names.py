# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The parenthesised job-name suffix of a matrix leg (`_resolved.job-name`), shared by ci.yml and generated lanes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# python-version renders as py<ver>; empty options as "default".
_PYTHON_FIELD = "python-version"
_OPTIONS_FIELD = "options"


def _leg_values(legs: Sequence[Mapping[str, Any]], key: str) -> set[Any]:
    return {tuple(v) if isinstance(v, list) else v for v in (leg.get(key) for leg in legs)}


def _compiler_fields(legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str]) -> list[str]:
    """Toolchain fields for the title, shown even when constant.

    `compiler-inputs` (else a template's `cxx`/`cc`); the varying ones if any vary.
    """
    fields = [f for f in sorted(compiler_inputs) if any(f in leg for leg in legs)]
    if not fields:
        fields = [f for f in ("cxx", "cc") if any(f in leg for leg in legs)][:1]
    varying = [f for f in fields if len(_leg_values(legs, f)) > 1]
    return varying or fields


def first_distinguishing_field(legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str] = ()) -> str | None:
    """The first field that varies across legs, ranked: artifact-name fields, then platform, then the rest.

    Ranking is an allowlist, so a repo-invented key (e.g. a job-script path) never outranks identity.
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
    return None


def display_fields(legs: Sequence[Mapping[str, Any]], compiler_inputs: Sequence[str] = ()) -> list[str]:
    """A kind's title fields: platform, toolchain, distinguishing field, python, options."""
    compilers = _compiler_fields(legs, compiler_inputs)
    fields = ["platform", *compilers]
    distinguishing = first_distinguishing_field(legs, compiler_inputs)
    if distinguishing is not None and distinguishing not in {"platform", _PYTHON_FIELD, _OPTIONS_FIELD, *compilers}:
        fields.append(distinguishing)
    if any(_PYTHON_FIELD in leg for leg in legs):
        fields.append(_PYTHON_FIELD)
    # Present on only some legs, so legs differing only by options still get distinct titles.
    if any(_OPTIONS_FIELD in leg for leg in legs):
        fields.append(_OPTIONS_FIELD)
    return fields


def _render(value: Any) -> str:
    """Render like a GitHub expression."""
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
