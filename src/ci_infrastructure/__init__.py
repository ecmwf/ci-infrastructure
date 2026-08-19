# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""ci-infrastructure: shared CI orchestration scripts and Composite Actions.

Each module here is a CLI; `pyproject.toml`'s `[project.scripts]` is the list.
The composite actions in `actions/` do not use those console scripts, though:
they invoke `$CI_INFRASTRUCTURE_PYTHON -m ci_infrastructure.<module>`, where
`$CI_INFRASTRUCTURE_PYTHON` is the venv interpreter provisioned by the
`ensure-infrastructure-present` composite.
"""
