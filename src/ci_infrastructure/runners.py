# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Runner-class aliases: the one place an org-wide runner label is written down.

A manifest leg names a runner *class* (`runs-on = "hpc-submit"`) rather than the
label of whichever scale set currently serves it. `resolve_deps` substitutes the
real label into the matrix JSON it emits, so the day the fleet is renamed the
change is this table, not fifteen `runs-on =` lines across five repos.

Deliberately not overridable from the environment or from `vars.*`: the value of
a single source of truth is that there is only one place to look. A label that is
not a key here passes through untouched, so every literal `runs-on` keeps working
and only the classes below are indirected.

`runs-on` is scheduling, not artifact identity (see `_SCHEDULING_FIELDS` in
generate_downstream_ci), so remapping it never changes an artifact name.
"""

from __future__ import annotations

from typing import Final

# class name -> the runner label that currently serves it.
#
# hpc-submit: the ARC scale set whose pods hold the cluster ssh identity and
# submit SLURM jobs through troika. It is NOT on the cluster — see HPC.md.
RUNNER_CLASSES: Final[dict[str, str]] = {
    "hpc-submit": "arc-hpc-pet-vsphere-prod",
}


def resolve_runner(runs_on: object) -> object:
    """Map runner classes to labels in a leg's `runs-on`.

    Accepts what a manifest may legally hold there: a scalar label, or a list of
    labels (the `["self-hosted", "linux", "hpc"]` form). Anything else — a value
    a future schema allows, or None from a leg that omits the field — is returned
    unchanged rather than guessed at.
    """
    if isinstance(runs_on, str):
        return RUNNER_CLASSES.get(runs_on, runs_on)
    if isinstance(runs_on, list):
        return [RUNNER_CLASSES.get(v, v) if isinstance(v, str) else v for v in runs_on]
    return runs_on
