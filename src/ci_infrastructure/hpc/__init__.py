# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""HPC (SLURM) execution backend for ci-infrastructure.

An HPC matrix leg is an ordinary leg whose build runs *inside a SLURM job*
rather than on the GitHub runner. A login-node self-hosted runner submits the
job, waits for it, and publishes the resulting artifact through the same
name-keyed S3 store the non-HPC path uses.

Orchestration (submit / poll / cancel) is pure Python driving troika's ``Site``
API directly — there is no shell-out to the troika CLI. The SLURM job script
itself is necessarily bash (it runs on a compute node); everything around it is
Python.
"""
