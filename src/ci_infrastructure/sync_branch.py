# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Which branch names take part in cross-repo branch matching.

The one rule for both directions: resolve_deps picks an upstream dependency's
same-named branch, and actions/pick-ref picks a downstream consumer's, only for a
sync branch. Any other name (master, develop, a feature branch) uses the manifest
ref, so an unrelated same-named branch elsewhere is never built by accident.

Stdlib-only: actions/pick-ref runs this with a stock interpreter.
Exit status 0 when the argument is a sync branch, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from typing import Final

SYNC_BRANCH_RE: Final = re.compile(r"^(?:sync-branch/|feature-sync/)")


def is_sync_branch(branch: str) -> bool:
    return bool(SYNC_BRANCH_RE.match(branch))


if __name__ == "__main__":
    sys.exit(0 if len(sys.argv) == 2 and is_sync_branch(sys.argv[1]) else 1)
