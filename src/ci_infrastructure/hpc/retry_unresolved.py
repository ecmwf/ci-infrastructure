# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""ssh / scp that retry when the host name does not resolve; troika runs these instead (see site.load_site).

The runner pods' DNS drops lookups of the cluster's host now and then, sometimes in several pods
at once, and a re-run always got through. Before resolving, ssh has read nothing from stdin, so
running it again is safe. Stdlib only, since every ssh call starts one.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Final

UNRESOLVED: Final = b"Could not resolve hostname"
DELAYS: Final = (10, 30)


def run(argv: Sequence[str], delays: Sequence[float] = DELAYS) -> int:
    """Run `argv`, again after each of `delays` while it fails to resolve the host; its stderr follows it."""
    for delay in [*delays, None]:
        proc = subprocess.Popen(argv, stderr=subprocess.PIPE)
        assert proc.stderr is not None
        err = proc.stderr.read()
        code = proc.wait()
        if code == 0 or UNRESOLVED not in err or delay is None:
            sys.stderr.buffer.write(err)
            sys.stderr.flush()
            return code
        line = err.decode(errors="replace").strip().splitlines()[0]
        print(f"{argv[0]}: {line}; retrying in {delay}s", file=sys.stderr, flush=True)
        time.sleep(delay)
    raise AssertionError("unreachable")


def ssh() -> None:
    sys.exit(run(["ssh", *sys.argv[1:]]))


def scp() -> None:
    sys.exit(run(["scp", *sys.argv[1:]]))
