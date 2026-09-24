# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""A runner class maps to its fleet label; anything else passes through."""

from __future__ import annotations

from typing import Any

import pytest

from ci_infrastructure.runners import RUNNER_CLASSES, resolve_runner


@pytest.mark.parametrize(
    ("label", "resolved"),
    [
        ("hpc-submit", RUNNER_CLASSES["hpc-submit"]),
        ("arc-runner-very-large", "arc-runner-very-large"),
        (["self-hosted", "hpc-submit"], ["self-hosted", RUNNER_CLASSES["hpc-submit"]]),
        (None, None),
        (7, 7),
    ],
)
def test_resolve_runner(label: Any, resolved: Any) -> None:
    assert resolve_runner(label) == resolved


def test_hpc_submit_label() -> None:
    assert RUNNER_CLASSES["hpc-submit"] == "arc-hpc-pet-vsphere-prod"
