# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""ssh / scp that retry when the host name does not resolve; troika runs these instead (see site.load_site).

The runner pods' DNS drops lookups of the cluster's host now and then, sometimes in several pods
at once, and a re-run always got through. Before resolving, ssh has read nothing from stdin, so
running it again is safe. An scp that stalls, its connection alive but no data moving, is killed
after ``SCP_TIMEOUT`` and run again; a copy is safe to repeat. Stdlib only, since every ssh call starts one.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Final

UNRESOLVED: Final = b"Could not resolve hostname"
DELAYS: Final = (10, 30)
#: Well above a normal copy (a couple of minutes for an install tree), far below a job's timeout.
SCP_TIMEOUT: Final = 600


def run(argv: Sequence[str], delays: Sequence[float] = DELAYS, timeout: float | None = None) -> int:
    """Run `argv`, again after each of `delays` while it fails to resolve the host or exceeds `timeout` seconds."""
    for delay in [*delays, None]:
        proc = subprocess.Popen(argv, stderr=subprocess.PIPE)
        stalled = f"no result after {timeout}s"
        try:
            _stdout, err = proc.communicate(timeout=timeout)
            reason = None
            if proc.returncode != 0 and UNRESOLVED in err:
                reason = err.decode(errors="replace").strip().splitlines()[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            _stdout, err = proc.communicate()
            reason = stalled
        if reason is None or delay is None:
            sys.stderr.buffer.write(err)
            if reason == stalled:
                print(f"{argv[0]}: {stalled}; giving up", file=sys.stderr)
            sys.stderr.flush()
            return proc.returncode
        print(f"{argv[0]}: {reason}; retrying in {delay}s", file=sys.stderr, flush=True)
        time.sleep(delay)
    raise AssertionError("unreachable")


def ssh() -> None:
    sys.exit(run(["ssh", *sys.argv[1:]]))


def scp() -> None:
    sys.exit(run(["scp", *sys.argv[1:]], timeout=SCP_TIMEOUT))
