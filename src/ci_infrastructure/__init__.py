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

"""ci-infrastructure: shared CI orchestration scripts and Composite Actions.

The CLI entry points are exposed as `ci-infrastructure-generate`, `ci-infrastructure-resolve`,
`ci-infrastructure-fetch`, `ci-infrastructure-check`, and `ci-infrastructure-print-dep-table` —
see `pyproject.toml`'s `[project.scripts]`. The composite actions in `actions/`
invoke these via `$CI_INFRASTRUCTURE_PYTHON -m ci_infrastructure.<module>`, where
`$CI_INFRASTRUCTURE_PYTHON` is the venv interpreter provisioned by the
`ensure-infrastructure-present` composite.
"""
