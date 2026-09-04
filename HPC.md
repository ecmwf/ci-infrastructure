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
`hpc` self-hosted runner (reaches the cluster only over troika ssh)
  resolve ─▶ fetch deps (S3) ─▶ build-on-hpc: submit ─▶ ship source + deps + marker ─▶ wait ─▶ fetch install ─▶ publish
                                                 │                                 ▲
                                                 ▼  troika (as a Python library)   │ Finished: SUCCESS/FAILURE
                                          SLURM compute node: wait for marker ─▶ unpack into $TMPDIR ─▶ .ci/hpc/build-<toolchain>.sh
```

- The job runs on the **`hpc` self-hosted runner** (`runs-on`), which submits
  the batch job, ships the source, waits for it, and publishes the result.
- Submission / polling / cancellation is **pure Python** driving troika's `Site`
  API directly (`ci_infrastructure.hpc`) — no shell-out to the troika CLI.
- **Submit-then-poll transfer.** The runner submits the job first (claiming its
  queue slot), then scp's the source tarball into the cluster staging dir and
  `touch`es the `TRANSFER_COMPLETED` marker there. The compute node blocks until
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
- **One shipper at a time.** Staging is keyed on the artifact alone, so two runs
  that want the same artifact (a repo's own CI and a fan-out) ship into one
  directory — and shipping starts by resetting that directory. A run therefore
  holds `<staging>.shiplock` (an atomic remote `mkdir`, a *sibling* of the staging
  dir because the reset renames the staging tree aside) for the whole ship, and
  re-checks the marker once it has the lock: the second run finds the first one's
  completed transfer and skips instead of deleting it mid-flight. Without it the
  loser's next remote untar fails on a tarball the winner just moved away
  (`deps/1.tgz: Cannot open: No such file or directory`). A lock left behind by a
  dead runner is broken after 30 minutes by the next shipper.
- Completion is detected by a `Finished: SUCCESS` / `Finished: FAILURE`
  **sentinel** in the job output (a completed job vanishes from `squeue`, so the
  scheduler is only used as a low-frequency liveness guard). The wait holds a
  single `tail -F | grep` connection, so it does not hammer the scheduler. It
  **fails closed**: only a sentinel actually read from the output is a verdict.
  Anything else — a job still queued (SLURM has not created the output yet), a
  dropped connection, a timeout — means "keep waiting", never "finished".

## Declaring an HPC kind

The repo owns its build recipe in `.ci/hpc/build-<toolchain>.sh` (its `#SBATCH` resource
directives, `module load` lines and cmake/ctest body). ci-infrastructure injects
`#SBATCH --output/--error`, the dependency environment
(`CMAKE_PREFIX_PATH` / `CI_INSTALL_PREFIX` / `CI_INSTALL_ARCHIVE`) and the sentinel footer.

**The recipe must end by writing `$CI_INSTALL_ARCHIVE`** — a gzipped tar of the install
tree, taken from wherever the recipe installed it:

```bash
mkdir -p "$(dirname "$CI_INSTALL_ARCHIVE")"
tar -cf - -C "$CI_INSTALL_PREFIX" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"
mv "$CI_INSTALL_ARCHIVE.part" "$CI_INSTALL_ARCHIVE"
```

zstd rather than gzip, `-T0` so it uses every core the job asked for; the smaller result
also comes back over the connection faster. `zstd` is in the public base image, so the
runner side can unpack it.

That is what `fetch_install` collects; it does not tar anything itself, and a job that
finishes without the archive fails the fetch. Archiving on the compute node keeps the
install tree off shared storage — for a tree of many small files, creating it there and
reading it back can cost more than the build — and compresses on the job's own cores. Use
`.part` + `mv` so the file only ever appears complete. A recipe whose install step supports
`DESTDIR` can go further and stage onto node-local disk, so the tree never reaches shared
storage at all (see `eccodes/.ci/hpc/build-gnu.sh`).

```toml
[[matrix.build.include]]
compiler = "gnu-12"
build-type = "Release"
platform = "hpc-atos-gnu"          # ABI class -> artifact slug (verbatim in the name)
runs-on = "hpc-submit"             # runner class, mapped in runners.RUNNER_CLASSES
site = "hpc-batch"                 # troika site from troika-config.yml (scheduling only)

[matrix.build]
execution = "hpc"                  # selects the SLURM path
job-script = "./.ci/hpc/build-gnu.sh"  # the repo-owned recipe (its own #SBATCH header)
triggers = ["upstream-change", "rebuild-request"]
forwarded-deps-outputs = ["cmake-prefix-path"]
needs = ["fortmath/build"]
```

`site` (like `runs-on`) is **scheduling, not identity** — two legs that differ
only by `site`/`runs-on` would publish under the same artifact name and are
rejected as a collision. Give an HPC build a distinct `platform` slug (e.g.
`hpc-atos-gnu`) since its toolchain is a different ABI from the runner images.
Slugs read general → detailed: lane, then site, then toolchain.

`runs-on` takes a **runner class** rather than a fleet label. The classes and the
labels they currently resolve to live in `src/ci_infrastructure/runners.py`;
`resolve_deps` substitutes the label into the emitted matrix, so renaming a scale
set org-wide is one edit there instead of one per manifest leg. A value that is
not a class passes through verbatim, so a literal label still works. There is
deliberately no environment or `vars.*` override — one source of truth means one
place to look.

Name a **plain** recipe after its toolchain (`build-gnu.sh`, `build-intel.sh`, …)
even when a repo has only one: `[matrix.<kind>] job-script` is a default, and a
file called `build.sh` that loads `prgenv/gnu` is a generic name doing specific
work. A **templated** recipe is the opposite case and is named `build.sh.j2`
precisely because it hardcodes no toolchain — see below.

See `samples/hpc/build-gnu.sh` for a plain recipe, `samples/hpc/build.sh.j2` for a
templated one.

## Templated recipes

A recipe whose path ends in **`.j2`** is rendered as a [Jinja][jinja] template
against the matrix leg that selected it, and the result is then wrapped exactly as
a plain recipe is. The suffix is the whole of the opt-in: any other path is read
verbatim, so every existing `build-gnu.sh` keeps working untouched.

[jinja]: https://jinja.palletsprojects.com/

This exists to close a real gap. A leg declares `cxx-compiler = "g++-8"` and
`build-type = "Release"` — the values the **artifact name** is built from — while
the recipe independently runs `module load gcc/old` and `-DCMAKE_BUILD_TYPE=Release`.
Nothing kept the two in agreement, so a module bump changed the binary without
changing the name, and the store then served an ABI-mismatched tree to every
consumer. With a template the leg is the only statement of the fact:

```toml
[[matrix.build-hpc.include]]
cxx-compiler = "g++-8"          # artifact-name label
build-type   = "Release"
platform     = "hpc-atos-gnu"   # ABI class -> the artifact name
# Ordered `module` SUB-COMMANDS, not names: a toolchain block interleaves an
# unload between two loads, which a list of names cannot express.
modules = ["load prgenv/gnu", "unload gcc", "load gcc/old", "load cmake"]
cc = "gcc"
cxx = "g++"

[matrix.build-hpc]
execution  = "hpc"
job-script = "./.ci/hpc/build.sh.j2"   # one recipe for every leg
```

```jinja
{% for m in modules %}
module {{ m }}
{% endfor %}
cmake -S "$CI_SOURCE_DIR" -B "${TMPDIR:-/tmp}/build" \
  -DCMAKE_BUILD_TYPE={{ build_type }} \
  -DCMAKE_CXX_COMPILER="$(command -v {{ cxx }})" \
  -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"
```

### What a template may reference

| name | is |
|---|---|
| `cxx_compiler`, `build_type`, `modules`, … | every leg key, **hyphens as underscores** (Jinja reads `{{ cxx-compiler }}` as a subtraction) |
| `leg['cxx-compiler']` | the raw mapping, for a key spelled exactly as the manifest does. Costs the generate-time check below |
| `artifact_name` | this build's identity |
| the `sh` filter | `shlex.quote`, for a value used as **one shell word** |

**Anything else is an error, never an empty string.** The environment uses
`StrictUndefined`, so a recipe reading `{{ fortran }}` on a leg that does not
declare it fails and says so. That is the enforcement — rendering it empty is
exactly the silent disagreement this feature removes. The same check runs
statically at `ci-infrastructure-generate` time, so it usually fails on a laptop
rather than half an hour into a SLURM queue. Being static, a name used only inside
a branch that is never taken must still be declared; `leg['x']` is invisible to it.

`| sh` is the only quoting tool a template has (autoescape is off — this is shell,
not markup). Do **not** apply it to a list of flags (`ctest-args`) or to a `module`
sub-command: quoting makes each one argument and breaks it.

`$CMAKE_PREFIX_PATH`, `$CI_INSTALL_PREFIX`, `$CI_INSTALL_ARCHIVE` and
`$CI_SOURCE_DIR` are **not** template names and `_resolved` is not in the context.
They are resolved on the cluster — the work dir may be `$SCRATCH/…`, and dependency
prefixes are re-shipped there and repointed *after* the leg is read. A template that
baked the runner-local value in would send the job at directories no compute node
can see. Keep writing `"$CI_INSTALL_PREFIX"`.

### Rendering one locally

`submit-wait --dryrun` is not an offline check — it still expands the remote work
dir over ssh. To see what a leg produces, with no cluster:

```bash
python -m ci_infrastructure.hpc render \
  --job-script .ci/hpc/build.sh.j2 \
  --matrix-leg '{"cc":"gcc","cxx":"g++","build-type":"Release","modules":["load prgenv/gnu"]}'
```

Diffing that against the recipe a template replaces is the check that a conversion
kept the build the same.

### The invariant a template does not enforce

The generator stops two *legs* colliding on one artifact name. It cannot stop a
*single* leg's `modules` changing from `gcc/old` to `gcc/11` while `platform` stays
`hpc-atos-gnu`: the name is unchanged, and the old artifact is still served from
cache. **Bump the `platform` slug when you change the toolchain** — that is what
invalidates the cache. The gain here is that the module set now lives in the same
four-line TOML table as `platform`, so this is a local, reviewable invariant on one
diff instead of a cross-file one nobody can see in a PR.

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
  user. **Recommended: `$SCRATCH/github-ci`.** The value may name cluster
  variables — it is expanded **on the cluster** (in a login shell, once per run)
  before any path is derived from it, so the configured value stays portable and
  needs no deploy username. The expansion must yield an absolute path; the step
  **fails fast** otherwise, since a relative or runner-local path is invisible to
  compute nodes and is the classic "wrong work directory" failure.
- **`vars.HPC_CI_WORK_DIR`** (a repo/org Actions *variable*): a **runner-local**
  scratch directory for the shipped/fetched tarballs and the fetched install
  tree. It need not survive across re-runs — reattach goes through the scheduler
  by job name, not a local file — so any writable dir works; if unset it falls
  back to `$RUNNER_TEMP`.
- **`secrets.HPC_CI_SSH_USER`** (optional): the remote/scheduler user for troika.

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
`ci-<artifact>`, and `submit-wait` finds an in-flight job by name (`squeue -n`)
before submitting. Because the scheduler is global, this dedups across
independent runners; a runner-local jid file cannot, so two runners would each
submit a duplicate.

The **name is the only thing read back**. A job also carries its submitting run
id in the SLURM `--comment`, but that is provenance for a human reading
`squeue`/`sacct` and is never parsed. `Comment` belongs to the scheduler and
sites rewrite it: ECMWF's `sbatch` wrapper appends its own accounting fields, so
a run id recovered from it arrives as `<run>-<attempt>;Gres=gres/ssdtmp:20G;` —
which contains a `/` and so cannot name a file at all. The transfer marker is
therefore named for the **staging dir**, which is already per-artifact — exactly
the scope the rendezvous needs — so a reattaching runner can ask "has anyone
finished shipping?" and re-drop the marker without knowing which run submitted
the job it adopted.

Cancelling the GitHub job scancels the batch job (a signal handler in
`submit-wait`), so a cancellation never orphans work on the cluster. troika has
no "restart" verb — restart is just re-submit, handled by the flow above.

## Cleanup

The submit-then-poll path leaves per-artifact `staging/`, `src/`, `install/` and
`hpc-jobs/` trees under `HPC_CI_REMOTE_WORK_DIR`. Two mechanisms reclaim them:

- **Opportunistic**: a fresh submit clears the artifact's staging dir before
  shipping (also removes any stale `TRANSFER_COMPLETED` marker), under the staging
  lock so it cannot clear a concurrent shipper's tree.
- **Nightly GC**: `.github/workflows/hpc-nightly-cleanup.yml` runs
  `python -m ci_infrastructure.hpc gc --remote-work-dir <…> --older-than-days N`
  on the `hpc` runner, sweeping per-artifact trees older than `N` days. Run
  it via `workflow_dispatch` with `dryrun: true` first to see what it would
  remove.

## Moving extra directories between the runner and the cluster

The build flow already brackets a job with two tree transfers over troika's
connection. The same transfer is also available standalone, for any workflow that
needs to move a directory in or out of the cluster outside a build — e.g. pulling
a job's reference/artifact directory back for a later processing step, or staging
inputs onto shared scratch before a job reads them. Both directions are a plain
ssh copy: **no scheduler and no S3**, so they work against `direct` sites
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

- it must live on a filesystem the **cluster** can reach (shared scratch — the
  same Lustre `$SCRATCH` the login node and the compute nodes both see). No
  runner is on the cluster, so nothing local is ever on that filesystem; the
  tree gets there by being pushed.
- `--remote-dir` is expanded **on the cluster**, so quote a `$SCRATCH/…` spec to
  keep the runner's shell from expanding it first (same rule as `--remote-work-dir`
  — see *Why the work dir is expanded on the cluster, not on the runner*).

As composite actions. `push-hpc-tree` writes the resolved cluster path as its
`remote-dir` output and `fetch-hpc-tree` writes `local-dir`, so later steps read
the resolved path rather than recomputing the spec. Neither needs a bootstrap
step: both run `ensure-infrastructure-present` themselves.

```yaml
- uses: ecmwf/ci-infrastructure/actions/push-hpc-tree@main
  id: push
  with:
    site: hpc-batch            # same troika site the job uses (or lumi)
    troika-user: ${{ secrets.HPC_CI_SSH_USER }}
    local-dir: ./inputs
    remote-dir: ${{ env.OUTPUT_DIR }}/inputs
```

```yaml
# post-step, to pull a job's output back to the runner
- uses: ecmwf/ci-infrastructure/actions/fetch-hpc-tree@main
  with:
    site: hpc-batch
    troika-user: ${{ secrets.HPC_CI_SSH_USER }}
    remote-dir: ${{ steps.push.outputs.remote-dir }}
    local-dir: ./ectrans-reference-artifact
```

Or directly, e.g. from a checkout, after `ensure-infrastructure-present`:

```bash
"$CI_INFRASTRUCTURE_PYTHON" -m ci_infrastructure.hpc fetch-tree --site hpc-batch \
  --remote-dir "$OUTPUT_DIR/ectrans-reference-artifact" \
  --local-dir ./ref --tar-dir "$RUNNER_TEMP/hpc-tars"
```

To reclaim scratch on success, add `remove-hpc-tree` as the **last** step and give
it **no** `if:`. A step with no `if:` runs only when every prior step succeeded, so
a failed job skips it and its trees stay on scratch for debugging (the nightly GC
sweeps them up later). Do **not** add `if: always()` — that would wipe the trees
on failure too.

```yaml
# last step of the job; no `if:` -> runs only if everything went green
- uses: ecmwf/ci-infrastructure/actions/remove-hpc-tree@main
  with:
    site: hpc-batch
    troika-user: ${{ secrets.HPC_CI_SSH_USER }}
    remote-dir: ${{ steps.push.outputs.remote-dir }}
```

`.github/workflows/smoke-test-hpc.yml` exercises all three actions against the
real cluster, on every push and pull request. pytest covers the same
ground without a cluster: `test_push_then_fetch_roundtrip_preserves_tree` does the
same tar → transfer → untar round-trip through a connection that really copies
bytes.
