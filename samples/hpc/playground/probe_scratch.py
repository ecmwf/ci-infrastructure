#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Report the cluster's scratch layout, from the login node and from a compute node.

Which directory can serve as ``HPC_CI_REMOTE_WORK_DIR`` is not a matter of
opinion: it must be writable from the **login node** (that is where the runner
scp's the source tarball and drops the transfer marker) and readable from the
**compute node** (that is where the job waits for that marker and unpacks). A
path that satisfies only the first is exactly the "wrong work directory" failure
this playground exists to rule out.

So this runs one probe body in both places and prints them side by side; the
difference between the two is the answer. The login pass leaves a stamp file in
every writable candidate, and the compute pass reports which stamps it can see —
that, not the mount table, is what proves a filesystem is shared.

It also answers the one question the orchestrator's own path resolution depends
on: whether ``$SCRATCH`` is set for a plain ssh shell or only for a login shell
(on ECMWF's atos it comes from ``ecprofile`` under ``/etc/profile.d``).

Usage::

    python probe_scratch.py --site hpc-batch --troika-user "$USER"
    python probe_scratch.py --site local-direct --where login   # no cluster

Writes only dot-prefixed stamp files, and removes them on the way out.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript
from ci_infrastructure.hpc.orchestrate import _remote_sentinel_waiter, wait_for_job, write_job_script
from ci_infrastructure.hpc.site import load_site, resolve_remote_path

#: Every directory worth considering, in the order the report prints them.
#: $TMPDIR/$SLURM_TMPDIR are expected to be node-local (fine as a build target,
#: useless as a staging dir); the rest are candidates for the work dir itself.
CANDIDATES = ("HOME", "TMPDIR", "SCRATCH", "SCRATCHDIR", "PERM", "HPCPERM", "SLURM_TMPDIR")


def _probe_body(candidates: tuple[str, ...], stamp: str, where: str) -> str:
    """The report script, run verbatim on the login node and on a compute node."""
    names = " ".join(candidates)
    # Only the compute pass can learn anything from the login pass's stamps; on
    # the login node it would just be reading back what it wrote a line earlier.
    shared_note = (
        "_shared_note() { :; }"
        if where == "login"
        else (
            "_shared_note() {\n"
            f'  if [ -e "$1/.probe-{stamp}-login" ]; then\n'
            '    printf ", sees the login node\'s stamp: YES (shared)"\n'
            "  else\n"
            '    printf ", sees the login node\'s stamp: no (node-local or not shared)"\n'
            "  fi\n"
            "}"
        )
    )
    return f"""
{shared_note}
echo "=============================================================="
echo "  probe: {where}"
echo "=============================================================="
echo "hostname : $(hostname)"
echo "user     : $(id -un)"
echo "slurm job: ${{SLURM_JOB_ID:-<not in a job>}}"
echo

echo "--- is \\$SCRATCH set without a login shell? -------------------"
echo "bash -c  : $(bash -c  'printf %s "${{SCRATCH:-<unset>}}"' 2>/dev/null)"
echo "bash -lc : $(bash -lc 'printf %s "${{SCRATCH:-<unset>}}"' 2>/dev/null)"
echo

echo "--- candidates ------------------------------------------------"
for _v in {names}; do
  eval "_val=\\${{$_v:-}}"
  if [ -z "$_val" ]; then
    printf '%-12s <unset>\\n' "$_v"
    continue
  fi
  # `cd && pwd -P` resolves symlinks without GNU realpath; on atos it is what
  # turns /scratch/<user> into its /ec/res4 or /lus mount.
  _real=$(cd "$_val" 2>/dev/null && pwd -P || echo '?')
  # Long options on purpose: BSD stat rejects them outright and falls back,
  # whereas `-f -c %T` there parses as a format string and prints nonsense.
  _fs=$(stat --file-system --format=%T "$_val" 2>/dev/null || echo '?')
  _free=$(df -Ph "$_val" 2>/dev/null | awk 'NR==2 {{print $4" free / "$2}}')
  if touch "$_val/.probe-{stamp}-{where}" 2>/dev/null; then
    _write='writable'
  else
    _write='NOT-writable'
  fi
  printf '%-12s %s\\n' "$_v" "$_val"
  printf '             real=%s fs=%s %s\\n' "$_real" "$_fs" "${{_free:-?}}"
  printf '             %s%s\\n' "$_write" "$(_shared_note "$_val")"
done
echo
"""


def _cleanup_body(candidates: tuple[str, ...], stamp: str) -> str:
    names = " ".join(candidates)
    return f"""
for _v in {names}; do
  eval "_val=\\${{$_v:-}}"
  [ -n "$_val" ] && rm -f "$_val"/.probe-{stamp}-* 2>/dev/null
done
true
"""


def _run_on_login(conn: Any, body: str) -> str:
    """Run the body over the very connection ship_source uses."""
    proc = conn.execute(["bash", "-lc", body], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, _ = proc.communicate()
    return stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)


def _run_on_compute(site: Any, body: str, *, args: argparse.Namespace, output: str) -> str:
    """Submit the body as a SLURM job and read its output back.

    Wrapped with the production renderer so the job carries the same sentinel
    footer the real builds use, and waited on with the production waiter.
    """
    header = "\n".join(
        [
            "#!/bin/bash",
            f"#SBATCH --qos={args.qos}" if args.qos else "",
            f"#SBATCH --time={args.time}",
            "#SBATCH --ntasks=1",
            f"#SBATCH --gres=ssdtmp:{args.ssdtmp}" if args.ssdtmp else "",
        ]
    )
    rendered = jobscript.render_job_script(
        repo_script=f"{header}\n{body}",
        output_path=output,
        cmake_prefix_path="",
        install_path="/dev/null",
    )
    script = Path(f"probe-job-{uuid.uuid4().hex[:8]}.sh")
    write_job_script(script, rendered)
    try:
        site.create_output_dir(output)
        # Same reason the orchestrator does this: the waiter must have a file to
        # follow, and an unwritable output should fail here, not silently.
        conn = site._connection
        conn.execute(["mkdir", "-p", str(Path(output).parent)], stdout=subprocess.PIPE).communicate()
        conn.execute(["touch", output], stdout=subprocess.PIPE).communicate()

        jid = int(site.submit(str(script), args.troika_user, output))
        print(f"probe: submitted job {jid}; waiting (queue time counts against --wait-timeout)...")
        started = time.monotonic()
        verdict = wait_for_job(
            sentinel_waiter=_remote_sentinel_waiter(site, output),
            state_getter=lambda: site._get_state(jid, strict=False),
            timeout=args.wait_timeout,
            guard_interval=15,
        )
        print(f"probe: job {jid} -> {verdict} after {time.monotonic() - started:.0f}s")
    finally:
        script.unlink(missing_ok=True)

    proc = conn.execute(["cat", output], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, _ = proc.communicate()
    return stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", default="hpc-batch", help="troika site (default: hpc-batch)")
    parser.add_argument("--troika-config", default=None, help="troika config path (default: packaged)")
    parser.add_argument("--troika-user", default=None, help="remote/scheduler user")
    parser.add_argument(
        "--where",
        choices=("login", "compute", "both"),
        default="both",
        help="run the probe on the login node, in a SLURM job, or both (default: both)",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="VAR",
        help="extra environment variable naming a directory to probe (repeatable)",
    )
    parser.add_argument(
        "--output-dir",
        default="$HOME/ci-probe",
        help="cluster dir for the probe job's output. $HOME is the one path we can "
        "assume is shared before the probe has told us anything (default: $HOME/ci-probe)",
    )
    parser.add_argument("--qos", default="nf", help="#SBATCH --qos for the probe job ('' to omit; default: nf)")
    parser.add_argument("--time", default="00:05:00", help="#SBATCH --time for the probe job")
    parser.add_argument("--ssdtmp", default="10G", help="#SBATCH --gres=ssdtmp size ('' to omit)")
    parser.add_argument("--wait-timeout", type=float, default=900, help="seconds to wait for the probe job")
    parser.add_argument("--keep-stamps", action="store_true", help="leave the .probe-* stamp files behind")
    args = parser.parse_args()

    candidates = CANDIDATES + tuple(args.candidate)
    stamp = uuid.uuid4().hex[:8]
    site = load_site(args.site, config_path=args.troika_config, user=args.troika_user)
    conn = site._connection

    reports: list[str] = []
    # Login first, always: the compute pass reports whether it can see the
    # stamps the login pass leaves, so the order is what makes that test mean
    # anything.
    if args.where in ("login", "both"):
        reports.append(_run_on_login(conn, _probe_body(candidates, stamp, "login")))

    if args.where in ("compute", "both"):
        output_dir = resolve_remote_path(conn, args.output_dir)
        output = f"{output_dir}/probe-{stamp}.out"
        reports.append(_run_on_compute(site, _probe_body(candidates, stamp, "compute"), args=args, output=output))

    print("\n".join(reports))

    if not args.keep_stamps:
        _run_on_login(conn, _cleanup_body(candidates, stamp))

    print("Read it like this:")
    print("  * HPC_CI_REMOTE_WORK_DIR needs a candidate that is writable on the login")
    print("    node AND whose login stamp the compute node can see.")
    print("  * A candidate that is writable but whose stamp compute cannot see is")
    print("    node-local -- fine as a build dir, useless for staging.")
    print("  * If $SCRATCH shows under 'bash -lc' but not 'bash -c', the login shell")
    print("    is required -- which is what resolve_remote_path() uses.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CIError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        sys.exit(1)
