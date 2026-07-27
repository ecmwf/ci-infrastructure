#!/usr/bin/env python3
"""Drive one full HPC build end to end, with no S3 and no GitHub.

This is the smallest thing that exercises the real path: resolve the work dir on
the cluster, submit, ship the source, drop the transfer marker, wait for the
sentinel, fetch the install tree back. Every step calls the same function the
`build-on-hpc` action calls, so a green run here means the orchestration works
and only the packaging around it is left.

It deliberately does *not* go through ``submit-wait``: that starts by asking S3
whether the artifact already exists, which needs credentials this playground has
no business requiring. It calls the library underneath instead.

Usage::

    python toy_roundtrip.py --remote-work-dir '$SCRATCH/downstream-ci-toy'

The work dir may name cluster variables — they are expanded on the cluster, the
same way the action does it. Everything is created under a unique per-run
artifact name and removed afterwards unless --keep.

Needs a real batch site (``hpc-batch``): the whole flow is built on submitting to
a scheduler and polling it. There is no offline mode — troika's ``direct`` sites
neither return a job id nor expose the state we poll.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from ci_infrastructure._errors import CIError
from ci_infrastructure.hpc import jobscript, transfer
from ci_infrastructure.hpc.orchestrate import (
    RemotePaths,
    _remote_sentinel_waiter,
    submit_or_reattach,
    wait_for_job,
    write_job_script,
)
from ci_infrastructure.hpc.site import ensure_batch_site, load_site, resolve_remote_path

HERE = Path(__file__).parent


def _make_source_tree(root: Path, token: str) -> Path:
    """A throwaway checkout whose content the job echoes back, proving the ship."""
    src = root / "src"
    src.mkdir(parents=True)
    (src / "hello.txt").write_text(f"shipped-from-the-runner token={token}\n")
    (src / "nested").mkdir()
    (src / "nested" / "deep.txt").write_text("nested files survive the tarball\n")
    return src


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", default="hpc-batch", help="troika site (default: hpc-batch)")
    parser.add_argument("--troika-config", default=None, help="troika config path (default: packaged)")
    parser.add_argument("--troika-user", default=None, help="remote/scheduler user")
    parser.add_argument(
        "--remote-work-dir",
        required=True,
        help="cluster work dir; may name cluster variables, e.g. '$SCRATCH/downstream-ci-toy'",
    )
    parser.add_argument("--job-script", default=str(HERE / "toy-build.sh"), help="recipe to submit")
    parser.add_argument("--wait-timeout", type=float, default=1800, help="seconds to wait for the job")
    parser.add_argument("--keep", action="store_true", help="do not remove the remote trees afterwards")
    args = parser.parse_args()

    token = uuid.uuid4().hex[:8]
    # Unique per run, and the staging dir is derived from it rather than taken
    # from the caller: ship_source() does `rm -rf` on the staging dir, so a
    # mistyped path must not be able to delete something real.
    artifact = f"playground-toy-{token}"
    run_id = f"toy-{token}"

    site = load_site(args.site, config_path=args.troika_config, user=args.troika_user)
    ensure_batch_site(site, args.site)
    conn = site._connection

    t0 = time.monotonic()
    work_dir = resolve_remote_path(conn, args.remote_work_dir)
    print(f"toy: work dir {args.remote_work_dir!r} -> {work_dir}")
    paths = RemotePaths.derive(work_dir, artifact)
    print(f"toy: output  {paths.output}")
    print(f"toy: staging {paths.staging}")
    print(f"toy: install {paths.install}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_dir = _make_source_tree(tmp_path, token)
        tar_dir = tmp_path / "tars"
        tar_dir.mkdir()
        local_install = tmp_path / "install"

        job_name = jobscript.job_name_for(artifact)
        rendered = jobscript.render_job_script(
            repo_script=Path(args.job_script).read_text(),
            output_path=paths.output,
            cmake_prefix_path="",
            install_path=paths.install,
            job_name=job_name,
            staging_dir=paths.staging,
            run_id=run_id,
        )
        script_path = tmp_path / f"job-{artifact}.sh"
        write_job_script(script_path, rendered)

        def ship() -> None:
            print(f"toy: [{time.monotonic() - t0:6.1f}s] shipping source + marker...")
            transfer.ship_source(
                conn,
                local_source_dir=str(source_dir),
                staging_dir=paths.staging,
                run_id=run_id,
                tar_dir=str(tar_dir),
            )
            print(f"toy: [{time.monotonic() - t0:6.1f}s] marker dropped; the job may now proceed")

        jid, action, _ = submit_or_reattach(
            site=site,
            script_path=script_path,
            user=args.troika_user,
            output=paths.output,
            job_name=job_name,
            after_submit=ship,
        )
        print(f"toy: [{time.monotonic() - t0:6.1f}s] {action} job {jid}; waiting for the sentinel...")

        verdict = wait_for_job(
            sentinel_waiter=_remote_sentinel_waiter(site, paths.output),
            state_getter=lambda: site._get_state(jid, strict=False),
            timeout=args.wait_timeout,
            guard_interval=15,
        )
        print(f"toy: [{time.monotonic() - t0:6.1f}s] job {jid} -> {verdict}")

        proc = conn.execute(["cat", paths.output], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate()
        print("\n--- job output ------------------------------------------------")
        print(out.decode(errors="replace") if isinstance(out, bytes) else out)
        print("---------------------------------------------------------------\n")

        if verdict != "SUCCESS":
            print(f"toy: FAILED ({verdict}). The remote trees are left in place for debugging.")
            return 1

        transfer.fetch_install(
            conn,
            remote_install_dir=paths.install,
            local_install_dir=str(local_install),
            tar_dir=str(tar_dir),
        )
        fetched = sorted(p for p in local_install.rglob("*") if p.is_file())
        print(f"toy: [{time.monotonic() - t0:6.1f}s] fetched {len(fetched)} file(s) back to the runner:")
        for p in fetched:
            print(f"  {p.relative_to(local_install)}")
        result = local_install / "bin" / "toy-result.txt"
        if not result.is_file():
            print("toy: FAILED — the install tree came back without bin/toy-result.txt")
            return 1
        print("\n--- fetched toy-result.txt ------------------------------------")
        print(result.read_text())
        print("---------------------------------------------------------------")

        if not args.keep:
            for path in (paths.staging, paths.install, paths.output):
                conn.execute(["rm", "-rf", path], stdout=subprocess.PIPE).communicate()
            print("toy: removed this run's remote trees (pass --keep to retain them)")

    print(f"toy: SUCCESS in {time.monotonic() - t0:.0f}s — submit-then-poll works on this cluster.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CIError as exc:
        print(f"toy: {exc}", file=sys.stderr)
        sys.exit(1)
