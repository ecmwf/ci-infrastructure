# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CI orchestration CLIs.

Composite actions run them as `$CI_INFRASTRUCTURE_PYTHON -m ci_infrastructure.<module>`
(the venv from `ensure-infrastructure-present`), not via the console scripts.
"""
