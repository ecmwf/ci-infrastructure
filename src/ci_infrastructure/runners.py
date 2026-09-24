# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Runner-class aliases (`runs-on = "hpc-submit"`), substituted by `resolve_deps`.

Deliberately not overridable from the environment.
"""

from __future__ import annotations

from typing import Final

#: hpc-submit: ARC pods that submit SLURM jobs through troika; not on the cluster.
RUNNER_CLASSES: Final[dict[str, str]] = {
    "hpc-submit": "arc-hpc-pet-vsphere-prod",
}


def resolve_runner(runs_on: object) -> object:
    """Map runner classes to labels in a scalar or list `runs-on`; anything else unchanged."""
    if isinstance(runs_on, str):
        return RUNNER_CLASSES.get(runs_on, runs_on)
    if isinstance(runs_on, list):
        return [RUNNER_CLASSES.get(v, v) if isinstance(v, str) else v for v in runs_on]
    return runs_on
