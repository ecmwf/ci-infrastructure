<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# HPC playground

Two operator scripts for poking at the cluster by hand. Nothing in CI runs them.

They exist to confirm — on the real cluster, as the deploy user — the two things
the HPC build path rests on:

1. **Which directory can be `HPC_CI_REMOTE_WORK_DIR`.** It must be writable from
   the *login node* (where the runner scp's the source tarball and drops the
   transfer marker) and readable from the *compute node* (where the job waits for
   that marker and unpacks). A path that is only the first is exactly the "wrong
   work directory" failure.
2. **That submit → ship → marker → build → fetch actually round-trips.**

## Setup

The scripts import `ci_infrastructure`, so use an interpreter that has it. On a
runner, `ensure-infrastructure-present` has usually already built one:

```bash
python="$(ls -d "$RUNNER_TOOL_CACHE"/ci-infrastructure-venv/*/bin/python | head -1)"
"$python" probe_scratch.py --help
```

Otherwise `pip install -e /path/to/ci-infrastructure` into a venv.

troika reaches the cluster over your ssh config, so `ssh hpc-batch true` must
work first. Pass `--troika-user` if the remote user differs from yours.

## 1. Where does scratch live?

```bash
python probe_scratch.py --site hpc-batch --troika-user "$HPC_USER"
```

Runs the same report on the login node and inside a SLURM job, and prints both.
It writes only dot-prefixed stamp files and cleans them up.

Read the output for:

| What you see | What it means |
|---|---|
| writable on login **and** compute sees the login stamp | a valid `HPC_CI_REMOTE_WORK_DIR` |
| writable, but compute does *not* see the login stamp | node-local — fine to build in, useless for staging |
| `$SCRATCH` under `bash -lc` but not `bash -c` | the login shell is required, which is what `resolve_remote_path()` uses |
| `fs=lustre` / `fs=nfs` | shared filesystem; `fs=tmpfs`/`ext4` on a compute node means node-local |

On ECMWF's atos this is expected to confirm `$SCRATCH = /ec/res4/scratch/<user>`
(Lustre, shared) and `$TMPDIR = /etc/ecmwf/ssd/ssd1/tmpdirs/<user>.<jobid>`
(node-local SSD, per job, sized by `#SBATCH --gres=ssdtmp:N`). If it does not,
the table above wins over this paragraph.

`--where login` alone needs no scheduler and works against any site (including
`local-direct`, i.e. your laptop). `--where compute` needs a queue slot, so it
can block; `--time`/`--qos`/`--ssdtmp` tune the probe job.

## 2. Does a real build round-trip?

```bash
python toy_roundtrip.py --site hpc-batch --troika-user "$HPC_USER" \
  --remote-work-dir '$SCRATCH/downstream-ci-toy'
```

**Quote the work dir.** It is expanded on the *cluster*; letting your shell
expand `$SCRATCH` first would silently send an empty string.

`toy-build.sh` has no cmake, no modules and no compiler, so a failure here can
only be the orchestration. Expect `toy-result.txt` to come back to the runner.
Adjust the `#SBATCH` lines in `toy-build.sh` first if the job never leaves
`PENDING`. Needs a batch site — `direct` sites (`hpc-login`) neither return a job
id nor expose the state we poll.

## 3. Then clean up after yourself

```bash
python -m ci_infrastructure.hpc gc --site hpc-batch \
  --remote-work-dir '$SCRATCH/downstream-ci-toy' --older-than-days 0 --dryrun
```

Drop `--dryrun` to actually remove. (`toy_roundtrip.py` already removes its own
trees unless you pass `--keep`.)

## Feeding the answer back

Once the probe agrees:

- `vars.HPC_CI_REMOTE_WORK_DIR` → the shared, compute-visible spec, e.g.
  `$SCRATCH/downstream-ci`. Keep it as a spec rather than a resolved path: it
  stays portable and keeps the deploy username out of repo variables.
- `vars.HPC_CI_WORK_DIR` → a stable **runner-local** path. It holds the persisted
  job ids, so `$RUNNER_TEMP` (the fallback) means reattach cannot survive a
  re-run.
