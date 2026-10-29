# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""CD v3 (experimental): build, test and publish releases from `[matrix.release-*]` kinds.

See docs/configuring/releases.rst.
"""

from .._errors import CIError


class ReleaseError(CIError):
    """A release that must not go ahead; the message says why. Shown as an ::error:: annotation."""
