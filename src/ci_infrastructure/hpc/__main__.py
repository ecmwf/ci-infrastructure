# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Enable ``python -m ci_infrastructure.hpc`` to reach the click CLI."""

from __future__ import annotations

from .orchestrate import main

if __name__ == "__main__":
    main()
