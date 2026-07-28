<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0

(C) Copyright 2026 - ECMWF and individual contributors.

This software is licensed under the terms of the Apache Licence Version 2.0
which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
In applying this licence, ECMWF does not waive the privileges and immunities
granted to it by virtue of its status as an intergovernmental organisation nor
does it submit to any jurisdiction.
-->

# HPC scratch-dir findings (ECMWF atos / hpc2020)

Probed on 2026-07-16 as user `<user>` from a login-node self-hosted runner,
using `probe_scratch.py` and `toy_roundtrip.py` against site `hpc-batch`. These
are the real-cluster answers the HPC build path was waiting on.

## The answer

```
vars.HPC_CI_REMOTE_WORK_DIR = $SCRATCH/downstream-ci
```

`$SCRATCH` expands to `/ec/res4/scratch/<user>`,
Lustre, ~49T free. It is **writable from the login node and compute-visible** —
the compute node saw the login node's stamp file — which is exactly the property
the staging + marker mechanism needs. Keep it as the spec `$SCRATCH/downstream-ci`
(not the resolved path): it stays portable and keeps the username out of repo vars.

## Filesystem map

| Variable | Resolves to | FS | Free | Shared to compute? | Role |
|---|---|---|---|---|---|
| `$SCRATCH` | `/ec/res4/scratch/<user>` | lustre | 49T | **yes** | ✅ `HPC_CI_REMOTE_WORK_DIR` |
| `$TMPDIR` | `/etc/ecmwf/ssd/ssd1/tmpdirs/<user>.<jobid>` | xfs (node SSD) | ~887G | no (node-local) | ✅ unpack + build target |
| `$PERM` | `/perm/<user>` | nfs | 56T | yes | shared but NFS — not for CI churn |
| `$HOME` | `/home/<user>` | nfs | 9T | yes | shared but NFS; used for probe bootstrap output |
| `$HPCPERM` | `/ec/res4/hpcperm/<user>` | lustre | 858G | yes | shared but too small |
| `$SCRATCHDIR` | `/ec/res4/scratchdir/<user>/<n>/<jobid>` | lustre | 49T | no (per-job) | purged per job |

Only `$TMPDIR` and `$SCRATCHDIR` are non-shared; `$TMPDIR` is the per-job
node-local SSD sized by `#SBATCH --gres=ssdtmp:N`, which is where
`jobscript.py` unpacks the source and where builds should run.

## What was confirmed about the mechanism

- **`$SCRATCH` resolves without a login shell.** Both `bash -c` and `bash -lc`
  print `/ec/res4/scratch/<user>`, so `resolve_remote_path()` is safe.
- **Marker visibility is effectively instant.** A login-node `touch` was already
  visible on the compute node when the job ran — the premise of submit-then-poll
  holds on Lustre.
- **`--qos=nf` with no explicit account/partition is correct.** The scheduler
  auto-assigned `Account: <ecmwf-account>`, `QOS: nf`. (LUMI, by contrast, needs
  `--account=<lumi-project> --partition=small`.)
- **Full round trip works.** `toy_roundtrip.py` submitted, shipped the source
  tarball, dropped the marker, the job unpacked into `$TMPDIR`, wrote
  `bin/toy-result.txt`, and it came back to the runner — all via the same
  library functions `build-on-hpc` calls. It removed its remote trees afterward.

## Observed timings

| Phase | Time |
|---|---|
| `probe_scratch.py --where both` (job: 16s queue + 4s run) | 22s wall |
| `toy_roundtrip.py` total | 42s |
|   ↳ resolve + ship source + marker | ~16s |
|   ↳ submit + queue + build + sentinel (job: 16s queue + 3s run) | ~15s |
|   ↳ fetch install back to runner | ~5s |

`nf` QoS queue time was ~16s both times — short-queue behaviour is fine for CI.

## Connection notes

- Site `hpc-batch` (troika slurm/ssh) reaches the cluster via the ssh host alias
  `hpc-batch`; the `<user>` user comes from the runner's ssh config.
- **Omit `--troika-user`** (or pass a real name). An *empty* string — e.g.
  `--troika-user "$HPC_USER"` with `HPC_USER` unset — makes troika build
  `ssh @hpc-batch`, which fails with ssh's usage message. Production's
  `build-on-hpc` already guards this (`if [ -n "$TROIKA_USER" ]`).

## Still to set / reconcile (operator)

- `vars.HPC_CI_REMOTE_WORK_DIR = $SCRATCH/downstream-ci` (above).
- `vars.HPC_CI_WORK_DIR` → a stable **runner-local** path (holds persisted job
  ids for reattach; the `$RUNNER_TEMP` fallback does not survive re-runs).
- Runner label: `build-package-hpc` targets `runs-on: [self-hosted, linux, hpc-dev]`,
  while `hpc-nightly-cleanup.yml` uses `[hpc]`. Confirm the label the dev runner
  actually registers with and align them, or jobs sit in "waiting for a runner".
