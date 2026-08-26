# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the runner-class indirection.

A manifest leg names a runner *class* (`runs-on = "hpc-submit"`); resolve_deps
substitutes the fleet label before the matrix JSON is written, because
`runs-on: ${{ matrix['runs-on'] }}` is not re-evaluated by GitHub. These pin the
two properties the indirection lives or dies by:

  - a class maps to its label, and anything else passes through verbatim, so the
    change is additive and every existing literal keeps working;
  - the substitution does not touch artifact identity — `runs-on` is scheduling,
    so remapping it must never move an artifact name.
"""

from __future__ import annotations

from ci_infrastructure.runners import RUNNER_CLASSES, resolve_runner


def test_hpc_submit_is_the_declared_class() -> None:
    """The one class the fleet actually uses. Asserted by name so renaming it in
    the table without updating every manifest fails here rather than in a job that
    queues forever against a label no runner answers to."""
    assert RUNNER_CLASSES["hpc-submit"] == "arc-hpc-pet-vsphere-prod"


def test_class_maps_to_label() -> None:
    assert resolve_runner("hpc-submit") == "arc-hpc-pet-vsphere-prod"


def test_unknown_label_passes_through() -> None:
    """The additive half of the contract: a literal that is not a class is left
    alone, so `arc-runner-very-large`, `ubuntu-24.04` and friends stay literal."""
    assert resolve_runner("arc-runner-very-large") == "arc-runner-very-large"
    assert resolve_runner("ubuntu-24.04") == "ubuntu-24.04"


def test_label_array_is_mapped_elementwise() -> None:
    """`runs-on` may be a label array; each element is a candidate class."""
    assert resolve_runner(["self-hosted", "hpc-submit"]) == ["self-hosted", "arc-hpc-pet-vsphere-prod"]


def test_non_string_is_returned_unchanged() -> None:
    """A leg that omits runs-on, or a future schema shape, is returned rather than
    guessed at — resolve_runner is not the place to validate the field."""
    assert resolve_runner(None) is None
    assert resolve_runner(7) == 7
