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
  there (not at the runner paths).
- **One shipper at a time.** Staging is keyed on the artifact alone, so two runs
  that want the same artifact (a repo's own CI and a fan-out) ship into one
  directory — and shipping starts by resetting that directory. A run therefore
  holds `<staging>.shiplock` (an atomic remote `mkdir`, a *sibling* of the staging
  dir because the reset renames the staging tree aside) for the whole ship, and
  re-checks the marker once it has the lock: the second run finds the first one's
  completed transfer and skips instead of deleting it mid-flight. A lock left
  behind by a dead runner is broken after 30 minutes by the next shipper.
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

**The recipe must end by writing `$CI_INSTALL_ARCHIVE`** — a zstd tar of the install
tree, taken from wherever the recipe installed it:

```bash
mkdir -p "$(dirname "$CI_INSTALL_ARCHIVE")"
tar -cf - -C "$CI_INSTALL_PREFIX" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"
mv "$CI_INSTALL_ARCHIVE.part" "$CI_INSTALL_ARCHIVE"
```

`fetch_install` collects that archive; a job that finishes without it fails the fetch.
Archiving on the compute node keeps a tree of many small files off shared storage, and
`.part` + `mv` makes the file appear only when complete. A recipe whose install step
supports `DESTDIR` can stage onto node-local disk instead (see
`eccodes/.ci/hpc/build-gnu.sh`).

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
not a class passes through verbatim, so a literal label still works.

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
a plain recipe is. Any other path is read verbatim.

[jinja]: https://jinja.palletsprojects.com/

The leg is then the single statement of the toolchain, so the recipe and the
**artifact name** cannot disagree:

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

**Anything else is an error, never an empty string** (`StrictUndefined`). The same
check runs statically at `ci-infrastructure-generate` time, so a name used only inside
a branch that is never taken must still be declared; `leg['x']` is invisible to it.

`| sh` is the only quoting tool a template has (autoescape is off — this is shell,
not markup). Do **not** apply it to a list of flags (`ctest-args`) or to a `module`
sub-command: quoting makes each one argument and breaks it.

`$CMAKE_PREFIX_PATH`, `$CI_INSTALL_PREFIX`, `$CI_INSTALL_ARCHIVE` and
`$CI_SOURCE_DIR` are **not** template names and `_resolved` is not in the context.
They are resolved on the cluster — the work dir may be `$SCRATCH/…`, and dependency
prefixes are re-shipped there and repointed *after* the leg is read. Keep writing
`"$CI_INSTALL_PREFIX"`.

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
invalidates the cache.

## Shared base template

Most packages build one CMake project the same way: load modules, configure, build,
test, install, archive. ci-infrastructure ships that recipe as
`ci-infrastructure/cmake-build.sh.j2`, and a package's `.ci/hpc/build.sh.j2` extends
it. This one line is a complete recipe:

```jinja
{% extends "ci-infrastructure/cmake-build.sh.j2" %}
```

A package that differs overrides only the blocks it needs; `{{ super() }}` keeps the
base's content.

| block | base content |
|---|---|
| `sbatch` | `--qos=nf`, `--nodes=1`, `--ntasks`, `--cpus-per-task` and `--mem` if set, `--gres=ssdtmp:`, `--time` from the leg |
| `preflight` | prints the compiler and cmake versions |
| `configure` | `cmake --preset <options or ci> -S "$CI_SOURCE_DIR" -B "$build"`, plus build type, compilers, rpath, prefix path and install prefix |
| `cmake_args` | empty, nested in `configure`; every line must end in ` \` |
| `build` | `cmake --build "$build" --parallel "$jobs"` |
| `test` | `ctest` with `ctest_args`, else `-j "$jobs"`; the whole block is skipped when `tests` is false |
| `install` | `cmake --install "$build"` |

After `install` the base tars `$install_root` into `$CI_INSTALL_ARCHIVE`. Blocks can
use these shell variables:

- `build`: the build directory on node-local disk.
- `jobs`: `$SLURM_CPUS_PER_TASK`, else `$SLURM_NTASKS`.
- `install_root`: `$CI_INSTALL_PREFIX` by default. A recipe that installs with
  `DESTDIR` points it at the staged tree.
- `gen_flag`: `-GNinja` when ninja is on `PATH`.

```jinja
{% extends "ci-infrastructure/cmake-build.sh.j2" %}
{% block install %}
DESTDIR="${TMPDIR:-/tmp}/stage" cmake --install "$build"
install_root="${TMPDIR:-/tmp}/stage$CI_INSTALL_PREFIX"
{% endblock %}
```

Text outside a block in a child is dropped. Put `{% extends %}` first and a licence
header in a `{# #}` comment.

### Defaults

The base reads a few settings that a leg does not have to declare. A leg's own key
wins. The defaults apply to every `.j2` recipe, not only to children of the base.

| key | default |
|---|---|
| `time` | `01:00:00` |
| `ntasks` | `8` |
| `cpus-per-task` | unset (no `--cpus-per-task` line) |
| `mem` | unset (no `--mem` line) |
| `ssdtmp` | `20G` |
| `tests` | `true` |
| `ctest-args` | `""` |
| `fc` | `""` (no Fortran compiler) |
| `options` | `""` |

To set a value once for every leg of one kind, put it in `[matrix.<kind>.defaults]`.
This works for any matrix, not only HPC. A leg's own key still wins:

```toml
[matrix.build-hpc.defaults]
build-type = "RelWithDebInfo"
ntasks = 2
```

### CMake presets

The base configures with `cmake --preset <options>`, or `--preset ci` for a leg
without `options`. The package's `CMakePresets.json` holds the feature flags, so the
runner lane's build action can configure from the same preset and the two lanes
cannot drift apart. Compilers, build type and paths stay on the command line. An
option preset inherits `ci`:

```json
{
  "version": 3,
  "cmakeMinimumRequired": {"major": 3, "minor": 21, "patch": 0},
  "configurePresets": [
    {"name": "ci", "cacheVariables": {"ENABLE_TESTS": "ON"}},
    {"name": "with-geo", "inherits": "ci", "cacheVariables": {"ENABLE_GEOGRAPHY": "ON"}}
  ]
}
```

Presets need CMake 3.21 or newer, so load a recent enough `cmake` module.

### Template version

Consumer workflows load ci-infrastructure `@main`. A change to the base template
therefore reaches every package without moving any sha, and the store would keep
serving artifacts built by the old recipe.

To prevent that, every hpc artifact name carries `HPC_TEMPLATE_VERSION`
(`_github_api.py`) as a `-hpcv<N>` segment after the build type. There is no
segment while the version is 0.

- **Enforcement:** changing the template fails a pinned test until the version is
  bumped.
- **Cost of a bump:** every hpc artifact rebuilds, including those of packages with
  their own recipe.
- **Just after a bump merges:** a consumer can look for the new name before its
  producer has rebuilt. The usual rebuild request then fills the gap.

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
the runner, so it silently becomes the empty string. So the spec is passed through verbatim and expanded once over the
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
before submitting; the scheduler is global, so this dedups across runners.

The **name is the only thing read back**. The submitting run id goes into the SLURM
`--comment` for humans only; sites rewrite `Comment`. The transfer marker is named
for the per-artifact **staging dir**, so a reattaching runner needs no run id.

Cancelling the GitHub job scancels the batch job (a signal handler in
`submit-wait`), so a cancellation never orphans work on the cluster. troika has
no "restart" verb — restart is just re-submit, handled by the flow above.

## Cleanup

The submit-then-poll path leaves per-artifact `staging/`, `install/`, `hpc-jobs/`,
`locks/` and `transfer-e2e/` trees under `HPC_CI_REMOTE_WORK_DIR`. Two mechanisms reclaim them:

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
the resolved path rather than recomputing the spec.

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
real cluster.
