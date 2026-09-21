<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# CI container images

The container images the downstream-CI jobs run inside, hosted on the ECMWF
Harbor registry **`eccr.ecmwf.int`**, project **`public-ci-images`**
(world-readable). They are built and pushed by
[`.github/workflows/images.yml`](https://github.com/ecmwf/ci-infrastructure/blob/main/.github/workflows/images.yml), which does its
work through [`build-image.sh`](https://github.com/ecmwf/ci-infrastructure/blob/main/build-image.sh), the entry point for
[`scripts/build_image.py`](https://github.com/ecmwf/ci-infrastructure/blob/main/scripts/build_image.py) (Python >= 3.11.4, stdlib only).

They live in this repo, next to the `ci_infrastructure` package the base image
installs, so an image and the sources inside it can never drift apart. The
secret-bearing private images are in
[`ecmwf/ci-container-images`](https://github.com/ecmwf/ci-container-images) and
follow the conventions on this page.

## Naming — platform / variant

```
public-images/<platform>/<variant>/Dockerfile
  <->
eccr.ecmwf.int/public-ci-images/<platform>-<variant>
```

The `/` becomes `-` because Harbor supports only two-level repository paths
(`project/repository`).

| Directory | Image |
|---|---|
| `public-images/ubuntu22.04/base` | `…/ubuntu22.04-base` |
| `public-images/ubuntu22.04/gcc11-gfortran11` | `…/ubuntu22.04-gcc11-gfortran11` |
| `public-images/ubuntu22.04/gcc11-gfortran11-boost-qt6` | `…/ubuntu22.04-gcc11-gfortran11-boost-qt6` |
| `public-images/ubuntu24.04/base` | `…/ubuntu24.04-base` |
| `public-images/ubuntu24.04/gcc13-gfortran13` | `…/ubuntu24.04-gcc13-gfortran13` |
| `public-images/ubuntu24.04/clang18` | `…/ubuntu24.04-clang18` |
| `public-images/ubuntu24.04/gcc13-gfortran13-boost-qt6` | `…/ubuntu24.04-gcc13-gfortran13-boost-qt6` |
| `public-images/ubuntu26.04/base` | `…/ubuntu26.04-base` |
| `public-images/ubuntu26.04/gcc15-gfortran15` | `…/ubuntu26.04-gcc15-gfortran15` |
| `public-images/ubuntu26.04/gcc15-gfortran15-boost-qt6` | `…/ubuntu26.04-gcc15-gfortran15-boost-qt6` |
| `public-images/rocky8/base` | `…/rocky8-base` |
| `public-images/rocky8/gcc8-gfortran8` | `…/rocky8-gcc8-gfortran8` |
| `public-images/rocky8/gcc8-gfortran8-boost-qt5` | `…/rocky8-gcc8-gfortran8-boost-qt5` |
| `public-images/rocky9/base` | `…/rocky9-base` |
| `public-images/rocky9/gcc11-gfortran11` | `…/rocky9-gcc11-gfortran11` |
| `public-images/rocky9/gcc11-gfortran11-boost-qt5` | `…/rocky9-gcc11-gfortran11-boost-qt5` |
| `public-images/rocky10/base` | `…/rocky10-base` |
| `public-images/rocky10/gcc14-gfortran14` | `…/rocky10-gcc14-gfortran14` |
| `public-images/rocky10/gcc14-gfortran14-boost-qt6` | `…/rocky10-gcc14-gfortran14-boost-qt6` |
| `public-images/debian11/base` | `…/debian11-base` |
| `public-images/debian11/gcc10-gfortran10` | `…/debian11-gcc10-gfortran10` |
| `public-images/debian11/gcc10-gfortran10-boost-qt5` | `…/debian11-gcc10-gfortran10-boost-qt5` |
| `public-images/debian12/base` | `…/debian12-base` |
| `public-images/debian12/gcc12-gfortran12` | `…/debian12-gcc12-gfortran12` |
| `public-images/debian12/gcc12-gfortran12-boost-qt6` | `…/debian12-gcc12-gfortran12-boost-qt6` |
| `public-images/debian13/base` | `…/debian13-base` |
| `public-images/debian13/gcc14-gfortran14` | `…/debian13-gcc14-gfortran14` |
| `public-images/debian13/gcc14-gfortran14-boost-qt6` | `…/debian13-gcc14-gfortran14-boost-qt6` |
| `public-images/fedora43/base` | `…/fedora43-base` |
| `public-images/fedora43/gcc15-gfortran15` | `…/fedora43-gcc15-gfortran15` |
| `public-images/fedora43/gcc15-gfortran15-boost-qt6` | `…/fedora43-gcc15-gfortran15-boost-qt6` |
| `public-images/fedora44/base` | `…/fedora44-base` |
| `public-images/fedora44/gcc16-gfortran16` | `…/fedora44-gcc16-gfortran16` |
| `public-images/fedora44/gcc16-gfortran16-boost-qt6` | `…/fedora44-gcc16-gfortran16-boost-qt6` |
| `public-images/rolling-arch/base` | `…/rolling-arch-base` |
| `public-images/rolling-arch/gcc-gfortran` | `…/rolling-arch-gcc-gfortran` |
| `public-images/rolling-arch/gcc-gfortran-boost-qt6` | `…/rolling-arch-gcc-gfortran-boost-qt6` |

`base` is the shared foundation (system packages, `cmake`, `make`, `gh`, Python
with its development headers, OpenSSL headers, and the `ci_infrastructure`
package) and **carries no compiler at all**. Every
other variant `FROM`s it **directly** and installs its own full toolchain — no
variant builds on another variant, because `images.yml` builds the base and then
all dependents in one parallel matrix, so a chain would race on `:latest`.
`scripts/build_image.py` refuses a deeper chain.

Boost and Qt are deliberately not in the base: both are large and wanted by one
package, so they live in the `gcc<N>-gfortran<N>-boost-qt<M>` variant, whose name says
which compiler and Qt major version it adds. Qt is there because ecflow builds
ecFlowUI by default (`ENABLE_UI=ON`) and its configure step requires a supported
Qt version. Most platforms provide Qt6; the exceptions below provide Qt5.

### The name is the whole toolchain

The bases carry no compiler, so an image has exactly what its name lists and
nothing is inferred from another token:

| token | asserts |
|---|---|
| `gcc<N>` | `gcc-N`, `g++-N`, both with working OpenMP |
| `clang<N>` | `clang-N`, `clang++-N`, both with working OpenMP |
| `gfortran<N>` | `gfortran-N`, with working OpenMP |
| `gcc`, `gfortran` | the same on rolling platforms, without a version check |

`verify-image.sh` checks both directions: a `base` must have no compiler on
`PATH`, and a variant naming no `gcc` must not have one — `gfortran-N` *Depends*
on `gcc-N`, so a GNU toolchain can arrive as another package's dependency and
become the `cc` a build picks up. That is also why a clang variant ships no
Fortran.

"Working OpenMP" means compiling, linking *and running* an OpenMP program. gcc
carries `omp.h` and `libgomp` with the compiler; clang splits them into
`libomp-<N>-dev`. A clang variant also needs `libstdc++-<N>-dev`, since clang++
compiles C++ against GCC's libstdc++ headers.

`debian11/base`, `debian12/base` and `ubuntu22.04/base` compile CPython from
source, so they install a toolchain and purge it again in the same image, ending
up compiler-free like the others.

## Platforms

Each platform carries the same three roles — `base`, a compiler variant, and a
boost+Qt variant — at whatever versions that distro actually ships. The version
is in the *name*, so what you get is never a surprise:

| Platform | gcc | Qt | boost | cmake | `CI_INFRASTRUCTURE_PYTHON` |
|---|---|---|---|---|---|
| `ubuntu22.04` | 11 | 6 | 1.74 | 3.31.6 | `/usr/local/bin/python3.11` (built from source) |
| `ubuntu24.04` | 12, 13 | 6 | 1.83 | 3.28 | `/usr/bin/python3` (3.12) |
| `ubuntu26.04` | 15 | 6 | 1.90 | 4.2 | `/usr/bin/python3` (3.14) |
| `rocky8` | 8.5 (distro default) | **5** | 1.66 | 3.26 | `/usr/bin/python3.12` |
| `rocky9` | 11 | **5** | 1.75 | 3.31 | `/usr/bin/python3.12` |
| `rocky10` | 14 | 6 | 1.83 | 3.31 | `/usr/bin/python3` (3.12) |
| `debian11` | 10 | **5** | 1.74 | 3.18 | `/usr/local/bin/python3.11` (built from source) |
| `debian12` | 12 | 6 | 1.74 | 3.31.6 | `/usr/local/bin/python3.11` (built from source) |
| `debian13` | 14 | 6 | 1.83 | 3.31 | `/usr/bin/python3` (3.13) |
| `fedora43` | 15 | 6 | 1.83 | 3.31 | `/usr/bin/python3` (3.14) |
| `fedora44` | 16 | 6 | 1.90 | 4.3 | `/usr/bin/python3` (3.14) |
| `rolling-arch` | newest | 6 | newest | newest | `/usr/bin/python3` |

Three consequences worth knowing before you pick one:

**Qt5, not Qt6, on `rocky8`, `rocky9`, and `debian11`.** These platforms use
their supported Qt5 packages rather than the Qt6 used by the other platforms.
ecflow's `cmake/Dependencies.cmake` accepts either, so these are real
substitutions, and the image name says which you get.

**`rocky8` uses the distro's own gcc, 8.5.** Not a `gcc-toolset-N` SCL under
`/opt/rh`: 8.5 is what the distro gives you by default and what the Atos HPC GNU
builds target, so it is the combination worth testing. It is old enough to need
care — `std::filesystem` still wants an explicit `-lstdc++fs`, which ecflow's
`cmake/CompilerOptions.cmake` already does for GNU < 9 — but C++17, the standard
every package here requires, is there. The variants symlink `gcc-8`/`g++-8`/
`gfortran-8` into `/usr/local/bin`, because the rocky RPMs ship only unversioned
names and every manifest asks CMake for a versioned one.

**`debian11`, `debian12` and `ubuntu22.04` build their own Python.**
`ci_infrastructure` needs >= 3.11.4 (`pyproject.toml`), but bullseye provides
Python 3.9, bookworm stops at 3.11.2, and Jammy's Python 3.11 package is only a
release candidate. These three bases therefore compile a pinned, checksummed
Python 3.11 from python.org with `--enable-shared` (for `find_package(Python3
COMPONENTS Development)`) and `make altinstall`, leaving the system interpreter
alone. These are the only images that fetch and build a CPython source tarball
directly; all pin its checksum for provenance and repeatable builds.

`ubuntu22.04`, `rocky8`, `rocky9`, `rocky10`, `debian11`, and `debian12` take
pytest from pip. On Ubuntu 22.04, Rocky 8, Rocky 9, Debian 11, and Debian 12,
the selected CI Python is newer than the distro's default interpreter and its
pytest package. Rocky 10 also installs pytest with the selected system Python's
pip. `ubuntu24.04`, `ubuntu26.04`, `debian13`, `fedora43`, `fedora44`, and
`rolling-arch` use their distro pytest package.

### `rolling-arch` — newest of everything, rebuilt nightly

Every other platform pins a distro release, so the stack meets a new gcc, cmake
or Qt only when someone bumps an image. `rolling-arch` tracks
upstream continuously, so a change that will reach the pinned platforms in a year
breaks *here* first, on a nightly build nobody is waiting on.

The **`rolling-` prefix** is load-bearing — see the tag rule below. It states the
guarantee (tracks upstream, rebuilt nightly) while the suffix names the distro
delivering it, so a second rolling platform is just another `rolling-*` directory.
`is_rolling()` matches the prefix, so nothing needs editing to add one; moving a
directory out of it silently turns the nightly rebuild off.

## The base installs this repo

```dockerfile
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN pip install /opt/ci-infrastructure && rm -rf /opt/ci-infrastructure
```

From the build context — which is the **repo root** — not over the network. There
is no pin file and no `git+https` fetch of our own sources: an image tagged `X`
contains commit `X` by construction.

The base also sets two `ENV`s: `CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3`, which
advertises the interpreter the package was baked into so
`ensure-infrastructure-present` can reuse it instead of building a per-job venv;
and `CI_INFRASTRUCTURE_BAKED_REF`, which records *which* commit was baked so a
running job can tell whether the image is current.

> `ensure-infrastructure-present` compares a digest of the baked package's `*.py`
> against the checkout's and, on a mismatch, **warns and installs from the
> checkout** into a venv. A PR job, or any job in the minutes after a merge to
> main, sees this until `images.yml` republishes the image.
> `CI_INFRASTRUCTURE_FORCE_REINSTALL=true` in the job's `env:` skips the baked
> interpreter outright (an env var, because a nested `uses:` cannot forward an input).

## Tagging — `<sha>` + `latest`

```
tag = git log -1 --format=%h -- <the image's build inputs>
```

The build inputs are the image's own directory, plus its base's directory if it
is a dependent, plus `src`, `pyproject.toml` and `LICENSE` — the paths the base
`COPY`s out of the context. The rule is *everything outside the image's own
directory that a Dockerfile reads from the context*; `scripts/build_image.py`'s
`EXTRA_TAG_PATHS` is where that list lives. `actions/` and `tests/` are
deliberately absent: actions are fetched by GitHub at job time and never baked,
and tests are not installed.

This over-approximates on purpose. An image that installed no `ci_infrastructure`
would still retag on every `src/` commit. Over-building is safe; under-building
is not.

Because the tag comes from **committed** content and never from `HEAD`, it moves
only when the image actually changes, and rebuilding is idempotent. Each push to
`main` also moves `:latest`, which is what downstream `manifest.toml` files and
`container:` blocks use, so they never need updating after a rebuild.

### There is exactly one answer to "does this need rebuilding?"

**An image is rebuilt when its tag is not already in the registry.** That is the
whole rule. `--discover` and the build path compute the tag through the same
functions in `scripts/build_image.py`, so they cannot disagree.

#### Rolling platforms change the tag, never the rule

An image under a `rolling-*` platform is not a function of our git history:
the same commit yields a different image every night. So its tag carries a UTC
date as well — `<sha>-<YYYYMMDD>` — and the rule above then does the right thing
unaided, because each night's tag is genuinely new and genuinely absent from the
registry. The nightly `schedule:` in `images.yml` discovers *every* image exactly
as a push does; the pinned platforms are already published and skipped.

A tag still names fixed bytes; nothing republishes one. `is_rolling()` in
`scripts/build_image.py` matches `rolling` or `rolling-*`, so there is no list.

The build jobs pin the tag `--discover` computed, via `IMAGE_TAG`. Without that,
a run straddling midnight UTC could discover `<sha>-20260902` as missing and then
publish `<sha>-20260903`.

Deliberately absent, and please keep them absent — each would be a second,
independent answer to the same question, to be kept in sync by hand:

- no `git diff` change-set against `github.event.before` or a PR base
- no `paths:` filter on the workflow
- no hand-maintained list of images anywhere (discovery is a glob)

When a second answer drifts from the tag, the existence check skips the build
and a dependent's `:latest` keeps pointing at an image built on the previous
base — a stale image that CI happily reports green.

On a pull request nothing is pushed, so "tag missing" means "this is what merging
would build" — exactly the set worth validating.

### Build, test, push

Every build job builds into the runner's docker daemon, runs
`build-image.sh --test` against that image, and only then pushes it — on `main`
only, and the very image that was tested, never a rebuild. `--test` runs
[`public-images/verify-image.sh`](https://github.com/ecmwf/ci-infrastructure/blob/main/public-images/verify-image.sh) (the image
contract: baked `ci_infrastructure`, `CI_IMAGE_*`, cmake floor, the compilers the
image's name promises and working OpenMP for each of them, the announcer) and
then `pytest` over `tests/` against the
**baked** package, with the checkout mounted read-only.

A dependent whose base is rebuilt in the same run builds on that base, not on the
published `:latest`: the base job exports it as an artifact, the dependent job
loads it and passes `BASE_IMAGE`, and `--test` checks the dependent's layers start
with the base's. On pull requests, `VALIDATE_DEPENDENTS_ON_PR` in `images.yml`
switches this off (`--mode validate-bases`), validating only the bases.

## Two-way jump (log ↔ Dockerfile)

**Image → source:** the tag *is* the short commit SHA, so browse
`…/blob/<sha>/public-images/<platform>/<variant>/Dockerfile`. When you hold a
digest rather than a tag, the labels have it:

```sh
skopeo inspect docker://eccr.ecmwf.int/public-ci-images/<platform>-<variant>:<tag> \
  | jq '.Labels | {dockerfile: ."int.ecmwf.ci.dockerfile",
                   revision:   ."org.opencontainers.image.revision"}'
```

**Source → image:** `eccr.ecmwf.int/public-ci-images/<platform>-<variant>`,
at the tag `./build-image.sh --print-tag <platform>/<variant>` prints.

**From inside a running container**, where no label is readable
(`$CI_INFRASTRUCTURE_BAKED_REF` also names the commit):

```sh
docker run --rm <image> env | grep ^CI_IMAGE_
```

In a GitHub Actions job this is printed for you — see *Self-description* below.

### Label set

`org.opencontainers.image.{source,revision,created,title,description}`, plus
`.base.name` on dependents, plus `int.ecmwf.ci.dockerfile`. The private repo
writes the same set by hand (and adds `.base.digest` and `int.ecmwf.ci.build.run`,
because its base is external and its content is not a pure function of git).

### Self-description

Labels answer "what is this image" only from **outside** — they need registry or
daemon access. A job running *inside* the image has neither, so the same facts
are baked in as environment:

| variable | |
|---|---|
| `CI_IMAGE_NAME` | `<platform>/<variant>` |
| `CI_IMAGE_TAG` | the tag it was published under |
| `CI_IMAGE_CREATED` | when these bytes were built (not when the commit landed) |
| `CI_IMAGE_DOCKERFILE_URL` | permalink to the exact Dockerfile |

`CI_IMAGE_TAG` and `CI_IMAGE_CREATED` are not redundant: one says *which* build,
the other *when it ran* — which is the pair you want when an upstream
`ubuntu:24.04` shifts underneath a pinned tag.

Two things print them, and a job gets exactly one announcement either way.

**Automatically, from the image.** Every base sets `BASH_ENV` to
`public-images/announce-image.sh`, and GitHub runs each step as
`bash --noprofile --norc -e -o pipefail`, which sources it. So *any* workflow in
*any* repo announces on its first bash step with nothing declared — including the
hand-written `ci.yml` files the generator never touches. It writes to **stderr**:
it runs inside whichever step is first, and if that step is not bash the job's
first bash can be a command substitution, whose stdout is a captured value rather
than the log. That rules out `::notice`, which the runner only parses on stdout.

**Explicitly, as a step.** `actions/announce-image` adds the `::notice`
annotation and takes an `extra` input for anything the caller knows that the image
does not (resolved dep refs, the matrix leg). The generator emits it into every
job it writes. It is pure bash and depends on nothing else in this repo — "which
image am I" must not fail for an unrelated reason.

The action sets `CI_IMAGE_ANNOUNCE_ACTION` on its own step, which stands the
script down for that step; the script otherwise claims the job by leaving
`/tmp/.ci-image-announced` and writing `CI_IMAGE_ANNOUNCED` to `GITHUB_ENV`. Both
are silent on runners outside these images (macOS legs), where `CI_IMAGE_NAME` is
simply unset, and a `sh` step announces nothing because `sh` ignores `BASH_ENV`.

> **In a hand-written `ci.yml`**, put `Announce image` first in every job that has
> a `container:`. The generator already does this for the workflows it writes.
> Forgetting it is not silent — `BASH_ENV` still announces — but the block then
> lands inside whichever step runs bash first, which may be one that goes on to
> produce twenty minutes of output.

This is as early as workflow-controlled code can be. The runner's own "Set up
job" block cannot be extended, and the job container's `ENTRYPOINT`/`CMD` never
run — GitHub starts it with its own command, which is why every base here ends
with `ENTRYPOINT []`.

Under ARC's Kubernetes mode "Initialize containers" does not name the image; see
[Runner-side hooks](../reference/runners.md#runner-side-hooks).

> **Inheritance cuts both ways.** Docker `ENV` is inherited, so an image that
> `FROM`s one of these **must re-declare the whole `CI_IMAGE_*` `ARG`/`ENV`
> block** or it will announce itself as its base, with a Dockerfile URL pointing
> at the wrong file. That is per-image *data*. The announcing script is
> *behaviour* that reads that data at runtime, so it is declared once in each
> base and inherited on purpose — do not repeat it in a variant.
> `smoke-test-runners.yml` asserts both halves. The re-declaration obligation
> falls on `ecmwf/ci-container-images` too, whose images `FROM` our base.

## Adding a new image

1. Create `public-images/<platform>/<variant>/Dockerfile`.
2. If it builds on the shared foundation, start with
   `FROM eccr.ecmwf.int/public-ci-images/<platform>-base:latest`.
   Copy the `CI_IMAGE_*` `ARG`/`ENV` block from any existing image — see
   *Self-description*; inheriting the base's is the one way to get it wrong.
3. Open a PR — the workflow discovers the new directory and validates it. On
   merge to `main` it is built and pushed.

No list to edit anywhere: discovery globs `public-images/*/*/Dockerfile`.

## Removing or renaming an image

Deleting the directory stops the image being built, but its repository stays in
the registry serving `:latest`, so a reference to the old name keeps working
silently instead of failing. Nothing prunes these.

[`scripts/registry_cleanup.py`](https://github.com/ecmwf/ci-infrastructure/blob/main/scripts/registry_cleanup.py) `orphans` lists
the repositories no Dockerfile here builds any more; the **Registry cleanup**
workflow runs it on demand (task `orphans`), and deletes them when dispatched
with `delete`. It never does so on its own schedule.
Deletion is irreversible, so check what still pulls a repository — Harbor's pull
count is on the report — before ticking it.

## Old versions and the project quota

Every publish adds a version and nothing else removes one, so without pruning the
project's 100 GiB quota fills and pushes fail with *"exceed the configured upper
limit"*. The **Registry cleanup** workflow runs
`scripts/registry_cleanup.py prune --delete` nightly, before the nightly image
build. It deletes all but the two most recently pushed versions of
every repository, and never one tagged `latest`. If a push was refused for quota,
dispatch it with `delete` and re-run the build; dispatched without `delete` it
only reports, and it takes a different `keep`. Anything
pinned to an older `<sha>` tag stops pulling once it has been pruned.

## Building by hand

The workflow and a workstation share one script, so both produce identical,
identically-tagged images. The build context is the repo root.

```sh
# validate locally (no push, no credentials needed)
./build-image.sh ubuntu24.04/base
./build-image.sh --test ubuntu24.04/base
BASE_IMAGE="$(./build-image.sh --print-tag ubuntu24.04/base | sed 's#^#eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:#')" \
  ./build-image.sh ubuntu24.04/gcc13-gfortran13

# what would be built right now, and under which tags
./build-image.sh --discover --mode publish
./build-image.sh --print-tag ubuntu24.04/clang18

# build and push (needs eccr network access + robot creds)
export PUBLIC_ECCR_ROBOT_NAME='robot$<project>+<purpose>'   # the Harbor push robot
export PUBLIC_ECCR_ROBOT_TOKEN=…
./build-image.sh ubuntu24.04/base --push
```

`docker buildx` is required — it is what queries the registry and what builds.
`--force` rebuilds past the existence check; `--require-clean` (used by the
publish leg in CI) refuses to publish from a tree that differs from the commit
the tag names.

## Required secrets

| Secret | Used for |
|---|---|
| `PUBLIC_ECCR_ROBOT_NAME` / `PUBLIC_ECCR_ROBOT_TOKEN` | pushing to `public-ci-images` |
| `PUBLIC_ECCR_CLEANUP_ROBOT_NAME` / `PUBLIC_ECCR_CLEANUP_ROBOT_TOKEN` | deleting from `public-ci-images` (registry prune and orphans); needs *Artifact* and *Repository* delete, no push |
| `CI_PERMISSIONS_APP_CLIENT_ID` / `CI_PERMISSIONS_APP_PRIVATE_KEY` | minting the `actions: write` token that dispatches the private image rebuild |

Reads are anonymous, so the discover job needs no secrets and works on pull
requests from forks.
