# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# (C) Copyright 2026 - ECMWF and individual contributors.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation nor
# does it submit to any jurisdiction.

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

from __future__ import annotations
