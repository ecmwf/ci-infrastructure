# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""A runner class maps to its fleet label; anything else passes through."""

from __future__ import annotations

from ci_infrastructure.runners import RUNNER_CLASSES, resolve_runner


def test_hpc_submit_maps_to_its_label() -> None:
    assert RUNNER_CLASSES["hpc-submit"] == "arc-hpc-pet-vsphere-prod"
    assert resolve_runner("hpc-submit") == "arc-hpc-pet-vsphere-prod"


def test_unknown_label_passes_through() -> None:
    assert resolve_runner("arc-runner-very-large") == "arc-runner-very-large"
    assert resolve_runner("ubuntu-24.04") == "ubuntu-24.04"


def test_label_array_is_mapped_elementwise() -> None:
    assert resolve_runner(["self-hosted", "hpc-submit"]) == ["self-hosted", "arc-hpc-pet-vsphere-prod"]


def test_non_string_is_returned_unchanged() -> None:
    assert resolve_runner(None) is None
    assert resolve_runner(7) == 7
