# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Which branch names take part in cross-repo branch matching (resolve_deps and actions/pick-ref).

Other names use the manifest ref, so an unrelated same-named branch is never built.
Stdlib-only: actions/pick-ref runs this with a stock interpreter. Exits 0 for a sync branch.
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
