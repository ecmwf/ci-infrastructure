<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# Running downstream CI on HPC (SLURM)

The HPC path lets a package build and test **inside a SLURM job** on a cluster,
while reusing the rest of the downstream-CI machinery unchanged: dependency
resolution, the Merkle-keyed artifact names, the S3 artifact store, the
cross-repo `needs` graph, and the check-run reporting. An HPC leg is just an
ordinary matrix leg with `execution = "hpc"`.

## How it fits together

```
login-node self-hosted runner
  resolve ─▶ fetch deps (S3) ─▶ build-on-hpc: submit ─▶ ship source + deps + marker ─▶ wait ─▶ fetch install ─▶ publish
                                                 │                                 ▲
                                                 ▼  troika (as a Python library)   │ Finished: SUCCESS/FAILURE
                                          SLURM compute node: wait for marker ─▶ unpack into $TMPDIR ─▶ .ci/hpc/build.sh
```

- The job runs on a **login-node self-hosted runner** (`runs-on`), which submits
  the batch job, ships the source, waits for it, and publishes the result.
- Submission / polling / cancellation is **pure Python** driving troika's `Site`
  API directly (`ci_infrastructure.hpc`) — no shell-out to the troika CLI.
- **Submit-then-poll transfer.** The runner submits the job first (claiming its
  queue slot), then scp's the source tarball into the cluster staging dir and
  `touch`es a `TRANSFER_COMPLETED_<run_id>` marker. The compute node blocks until
  the marker appears, unpacks the checkout into node-local `$TMPDIR` and builds
  there. The runner owns the checkout and the S3 store; **no GitHub token or
  egress lives on the cluster**. A reattach re-checks the marker and re-ships only
  if it is missing.
- **Dependency prefixes are shipped too.** The `cmake-prefix-path` a repo's
  download-only step produces points at runner-local dep install trees the compute
  node cannot see. So each is tarred and unpacked into `<staging>/deps/<i>` on the
  shared filesystem before the marker, and the job's `CMAKE_PREFIX_PATH` is pointed
  there (not at the runner paths). A build with no deps (e.g. stack-deps) ships
  nothing extra and its job script is unchanged.
- Completion is detected by a `Finished: SUCCESS` / `Finished: FAILURE`
  **sentinel** in the job output (a completed job vanishes from `squeue`, so the
  scheduler is only used as a low-frequency liveness guard). The wait holds a
  single `tail -F | grep` connection, so it does not hammer the scheduler. It
  **fails closed**: only a sentinel actually read from the output is a verdict.
  Anything else — a job still queued (SLURM has not created the output yet), a
  dropped connection, a timeout — means "keep waiting", never "finished".

## Declaring an HPC kind

The repo owns its build recipe in `.ci/hpc/build.sh` (its `#SBATCH` resource
directives, `module load` lines and cmake/ctest body). ci-infrastructure injects
`#SBATCH --output/--error`, the dependency environment
(`CMAKE_PREFIX_PATH` / `CI_INSTALL_PREFIX`) and the sentinel footer.

```toml
[[matrix.build.include]]
compiler = "gnu-12"
build-type = "Release"
platform = "atos-hpc-gnu"          # ABI class -> artifact slug (verbatim in the name)
runs-on = ["self-hosted", "linux", "hpc"]  # login-node self-hosted runner labels (scheduling only)
site = "hpc-batch"                 # troika site from troika-config.yml (scheduling only)

[matrix.build]
execution = "hpc"                  # selects the SLURM path
job-script = "./.ci/hpc/build.sh"  # the repo-owned recipe (with its own #SBATCH header)
triggers = ["upstream-change", "rebuild-request"]
forwarded-deps-outputs = ["cmake-prefix-path"]
needs = ["fortmath/build"]
```

`site` (like `runs-on`) is **scheduling, not identity** — two legs that differ
only by `site`/`runs-on` would publish under the same artifact name and are
rejected as a collision. Give an HPC build a distinct `platform` slug (e.g.
`atos-hpc-gnu`) since its toolchain is a different ABI from the runner images.

See `samples/hpc/build.sh` for a template recipe.

## Org-level configuration

- **Troika sites** live in `src/ci_infrastructure/hpc/troika-config.yml` (shipped
  with the package). Point `site` at one of them; pass `--troika-config` to
  override. troika reaches the cluster over ssh using the runner's ssh config.
  Only **batch (slurm)** sites work: the flow needs a job id, a named-job lookup
  to reattach by, and a scheduler state to poll, and troika's `direct` sites
  provide none of these.
- **`vars.HPC_CI_REMOTE_WORK_DIR`** (a repo/org Actions *variable*): the base
  directory on the cluster for the staged source, job output and the install
  tree, on a filesystem visible to the compute nodes and writable by the troika
  user. **Recommended: `$SCRATCH/downstream-ci`.** The value may name cluster
  variables — it is expanded **on the cluster** (in a login shell, once per run)
  before any path is derived from it, so the configured value stays portable and
  needs no deploy username. The expansion must yield an absolute path; the step
  **fails fast** otherwise, since a relative or runner-local path is invisible to
  compute nodes and is the classic "wrong work directory" failure.
- **`vars.HPC_CI_WORK_DIR`** (a repo/org Actions *variable*): a **runner-local**
  scratch directory for the shipped/fetched tarballs and the fetched install
  tree. It no longer needs to survive across re-runs — reattach goes through the
  scheduler by job name, not a local file — so any writable dir works; if unset
  it falls back to `$RUNNER_TEMP`.
- **`secrets.TROIKA_USER`** (optional): the remote/scheduler user for troika.

### Why the work dir is expanded on the cluster, not on the runner

The runner and the compute node need the *same* directory, but only one of them
knows where it is. troika `shlex.quote`s every argv element, so a `$SCRATCH`
handed to `mkdir`/`scp` arrives literally and creates a directory named
`$SCRATCH`. Expanding it in the workflow instead is worse: `$SCRATCH` is unset on
the runner, so it silently becomes the empty string (this is a live bug in the
reference implementation, whose `--workdir=$SCRATCH` legs quietly run somewhere
else entirely). So the spec is passed through verbatim and expanded once over the
connection — a login shell, because on atos `$SCRATCH` comes from `ecprofile` in
`/etc/profile.d`. Paths that only the *job* uses (`$TMPDIR`) need none of this:
they are written into the job script and expanded by the compute node's bash.

### Cluster layout (ECMWF atos / hpc2020)

| var | value | kind | persistence |
|---|---|---|---|
| `$SCRATCH` | `/ec/res4/scratch/<user>` | Lustre, shared | ~30-day purge |
| `$TMPDIR` | `/etc/ecmwf/ssd/ssd1/tmpdirs/<user>.<jobid>` | node-local SSD | per job, empty at start |
| `$HPCPERM` | `/ec/res4/hpcperm/<user>` | Lustre, shared | permanent |
| `$PERM` | `/perm/<user>` | NFS, shared | permanent |

The rule: **stage and install on `$SCRATCH`, build in `$TMPDIR`.** Only the
unpack/build target is node-local; the marker, the tarball and the job output
must be on the shared filesystem or the two sides cannot meet. `$TMPDIR` is sized
by `#SBATCH --gres=ssdtmp:N` — without it, the directory the job unpacks into may
be far smaller than the build needs. atos selects on **QoS** (`--qos=nf`), not
`--partition`.

## Restart / idempotency

Re-running a job is safe and cheap:

1. if the artifact already exists in the store → **skip** (cache hit);
2. else if a SLURM job for this artifact is still active → **reattach**, re-check
   its transfer marker (re-ship the source only if it is missing), and wait (no
   duplicate submission);
3. else **submit** fresh.

Reattach uses the **scheduler** as the shared job store: each job is named
`ci-<artifact>` and stamps its submitting run id into the SLURM `--comment`, so
`submit-wait` finds an in-flight job by name (`squeue -n`) before submitting.
Because the scheduler is global, this dedups across independent runners — the old
runner-local jid file could not, so two runners could each submit a duplicate.

Cancelling the GitHub job scancels the batch job (a signal handler in
`submit-wait`), so a cancellation never orphans work on the cluster. troika has
no "restart" verb — restart is just re-submit, handled by the flow above.

## Cleanup

The submit-then-poll path leaves per-artifact `staging/`, `src/`, `install/` and
`hpc-jobs/` trees under `HPC_CI_REMOTE_WORK_DIR`. Two mechanisms reclaim them:

- **Opportunistic**: a fresh submit clears the artifact's staging dir before
  shipping (also removes stale `TRANSFER_COMPLETED_*` markers).
- **Nightly GC**: `.github/workflows/hpc-nightly-cleanup.yml` runs
  `python -m ci_infrastructure.hpc gc --remote-work-dir <…> --older-than-days N`
  on the login-node runner, sweeping per-artifact trees older than `N` days. Run
  it via `workflow_dispatch` with `dryrun: true` first to see what it would
  remove.

## Moving extra directories between the runner and the cluster

The build flow already brackets a job with two tree transfers over troika's
connection. The same transfer is also available standalone, for any workflow that
needs to move a directory in or out of the cluster outside a build — e.g. pulling
a job's reference/artifact directory back for a later processing step, or staging
inputs onto shared scratch before a job reads them. Both directions are a plain
login-node copy: **no scheduler and no S3**, so they work against `direct` sites
too.

- **`fetch-tree`** — cluster → runner. Tars `--remote-dir` on the cluster, brings
  the single tarball back and unpacks it into `--local-dir` on the runner. Writes
  the runner-local directory as the `local-dir` output.
- **`push-tree`** — runner → cluster. Tars `--local-dir` on the runner, ships it up
  and unpacks it into `--remote-dir` on the cluster. Writes the resolved cluster
  directory as the `remote-dir` output.
- **`remove-tree`** — reclaim. `rm -rf`s `--remote-dir` on the cluster (and its
  sibling `.push.tgz` / `.fetch.tgz` transfer tarballs). Call it on success to
  return scratch a job's output dir once you no longer need it; it refuses to
  remove a top-level path.

Two rules for the remote directory:

- it must live on a filesystem the login node can reach (shared scratch — the same
  Lustre `$SCRATCH` the login-node runner and the compute nodes all see);
- `--remote-dir` is expanded **on the cluster**, so quote a `$SCRATCH/…` spec to
  keep the runner's shell from expanding it first (same rule as `--remote-work-dir`
  — see *Why the work dir is expanded on the cluster, not on the runner*).

As composite actions (post-step to pull a job's output back to the runner):

```yaml
- uses: ecmwf/ci-infrastructure/actions/fetch-hpc-tree@main
  with:
    site: hpc-batch            # same troika site the job used (or lumi)
    troika-user: ${{ secrets.HPC_CI_SSH_USER }}
    remote-dir: ${{ env.OUTPUT_DIR }}/ectrans-reference-artifact
    local-dir: ./ectrans-reference-artifact
```

```yaml
- uses: ecmwf/ci-infrastructure/actions/push-hpc-tree@main
  with:
    site: hpc-batch
    troika-user: ${{ secrets.HPC_CI_SSH_USER }}
    local-dir: ./inputs
    remote-dir: ${{ env.OUTPUT_DIR }}/inputs
```

To reclaim scratch on success, add `remove-hpc-tree` as the **last** step and give
it **no** `if:`. A step with no `if:` runs only when every prior step succeeded, so
a failed job skips it and its trees stay on scratch for debugging (the nightly GC
sweeps them up later). Do **not** add `if: always()` — that would wipe the trees on
failure too.

```yaml
# last step of the job; no `if:` -> runs only if everything went green
- uses: ecmwf/ci-infrastructure/actions/remove-hpc-tree@main
  with:
    site: hpc-batch
    troika-user: ${{ secrets.HPC_CI_SSH_USER }}
    remote-dir: ${{ env.OUTPUT_DIR }}/ectrans-reference-artifact
```

Or directly, e.g. on the login-node runner:

```bash
python -m ci_infrastructure.hpc fetch-tree --site hpc-batch \
  --remote-dir "$OUTPUT_DIR/ectrans-reference-artifact" \
  --local-dir ./ref --tar-dir "$RUNNER_TEMP/hpc-tars"
```

`.github/workflows/hpc-transfer-e2e.yml` exercises both commands and both actions
against the real cluster, on manual dispatch. Per-push coverage is pytest's:
`test_push_then_fetch_roundtrip_preserves_tree` does the same tar → transfer →
untar round-trip through a connection that really copies bytes.
