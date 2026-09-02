<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# CI container images

The container images the downstream-CI jobs run inside, hosted on the ECMWF
Harbor registry **`eccr.ecmwf.int`**, project **`public-ci-images`**
(world-readable). They are built and pushed by
[`.github/workflows/images.yml`](.github/workflows/images.yml), which does its
work through [`build-image.sh`](build-image.sh).

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
| `public-images/ubuntu24.04/base` | `…/ubuntu24.04-base` |
| `public-images/ubuntu24.04/gfortran12` | `…/ubuntu24.04-gfortran12` |
| `public-images/ubuntu24.04/gfortran13` | `…/ubuntu24.04-gfortran13` |
| `public-images/ubuntu24.04/clang18-gfortran12` | `…/ubuntu24.04-clang18-gfortran12` |
| `public-images/ubuntu24.04/clang18-gfortran13` | `…/ubuntu24.04-clang18-gfortran13` |
| `public-images/ubuntu24.04/gfortran13-boost-qt6` | `…/ubuntu24.04-gfortran13-boost-qt6` |
| `public-images/rocky8/base` | `…/rocky8-base` |
| `public-images/rocky8/gfortran13` | `…/rocky8-gfortran13` |
| `public-images/rocky8/gfortran13-boost-qt5` | `…/rocky8-gfortran13-boost-qt5` |
| `public-images/debian11/base` | `…/debian11-base` |
| `public-images/debian11/gfortran10` | `…/debian11-gfortran10` |
| `public-images/debian11/gfortran10-boost-qt5` | `…/debian11-gfortran10-boost-qt5` |
| `public-images/rolling-arch/base` | `…/rolling-arch-base` |
| `public-images/rolling-arch/gfortran` | `…/rolling-arch-gfortran` |
| `public-images/rolling-arch/gfortran-boost-qt6` | `…/rolling-arch-gfortran-boost-qt6` |

`base` is the shared foundation (system packages, `cmake`, `gh`, Python with its
development headers, OpenSSL headers, and the `ci_infrastructure` package). Every
other variant `FROM`s it **directly** and installs its own full toolchain — no
variant builds on another variant, because `images.yml` builds the base and then
all dependents in one parallel matrix, so a chain would race on `:latest`.
`build-image.sh` refuses a chain deeper than that rather than let it race.

Boost and Qt are deliberately not in the base: both are large and wanted by one
package, so they live in `gfortran13-boost-qt6`, whose name then says exactly
what it adds. Qt is there because ecflow builds ecFlowUI by default
(`ENABLE_UI=ON`) and its configure step is a hard error without Qt6.

## Platforms

Each platform carries the same three roles — `base`, a compiler variant, and a
boost+Qt variant — at whatever versions that distro actually ships. The version
is in the *name*, so what you get is never a surprise:

| Platform | gcc | Qt | boost | cmake | `CI_INFRASTRUCTURE_PYTHON` |
|---|---|---|---|---|---|
| `ubuntu24.04` | 12, 13 | 6 | 1.83 | 3.28 | `/usr/bin/python3` (3.12) |
| `rocky8` | 13 (SCL toolset) | **5** | 1.66 | 3.26 | `/usr/bin/python3.12` |
| `debian11` | 10 | **5** | 1.74 | 3.18 | `/usr/local/bin/python3.11` (built from source) |
| `rolling-arch` | newest | 6 | newest | newest | `/usr/bin/python3` |

Three consequences worth knowing before you pick one:

**Qt5, not Qt6, on `rocky8` and `debian11`.** Neither distro has Qt6 at all —
bullseye predates it, and rocky 8 has it in no repo. ecflow's
`cmake/Dependencies.cmake` accepts either, so these are real substitutions, and
the image name says which you get.

**`rocky8`'s gcc is an SCL toolset.** Rocky 8's own gcc is 8.5; 13 lives under
`/opt/rh/gcc-toolset-13`. The variants put it on `PATH` with `ENV` rather than
relying on `scl enable` or `BASH_ENV`, because those only fire for a shell that
reads them and `sh -c` does not.

**`debian11` builds its own Python.** `ci_infrastructure` needs >= 3.11
(`pyproject.toml`) and bullseye's ceiling is 3.10, backports included. The base
compiles a pinned, checksummed 3.11 with `--enable-shared` (for
`find_package(Python3 COMPONENTS Development)`) and `make altinstall`, leaving
the system 3.9 alone. It is the only image here that fetches anything from
outside a distro archive.

`rocky8` and `debian11` also take their pytest from pip rather than the distro,
because in both cases the distro package targets an interpreter (3.6, 3.9) that
is not the one those images run anything with. `ubuntu24.04` and `rolling-arch` use
the distro package.

### `rolling-arch` — newest of everything, rebuilt nightly

Every other platform pins a distro release, so the stack meets a new gcc, cmake
or Qt only when someone bumps an image. `rolling-arch` tracks
upstream continuously, so a change that will reach the pinned platforms in a year
breaks *here* first, on a nightly build nobody is waiting on. It is already a
useful canary: it currently carries gcc 16 and **cmake 4**, which no longer
accepts `cmake_minimum_required(VERSION < 3.5)`.

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
> against the checkout's and **fails on a mismatch**. On a pull request the
> published image necessarily lags the branch under test, so any PR job running
> inside one of these images must pass `force-reinstall: true`.

## Tagging — `<sha>` + `latest`

```
tag = git log -1 --format=%h -- <the image's build inputs>
```

The build inputs are the image's own directory, plus its base's directory if it
is a dependent, plus `src`, `pyproject.toml` and `LICENSE` — the paths the base
`COPY`s out of the context. The rule is *everything outside the image's own
directory that a Dockerfile reads from the context*; `build-image.sh`'s
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
functions in `build-image.sh`, so they cannot disagree.

#### Rolling platforms change the tag, never the rule

An image under a `rolling-*` platform is not a function of our git history:
the same commit yields a different image every night. So its tag carries a UTC
date as well — `<sha>-<YYYYMMDD>` — and the rule above then does the right thing
unaided, because each night's tag is genuinely new and genuinely absent from the
registry. The nightly `schedule:` in `images.yml` discovers *every* image exactly
as a push does; the pinned platforms are already published and skipped.

Note what this deliberately is **not**. It is not a second answer to the rebuild
question. And it is not a forced rebuild republishing one tag with new content,
which would break the guarantee that a tag names fixed bytes — the guarantee the
whole "Two-way jump" section below rests on. Nothing in `images.yml` names the
rolling images; `build-image.sh`'s `is_rolling()` matches any platform named
`rolling` or `rolling-*`, so there is still no list anywhere.

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
would build" — exactly the set worth validating. Dependents whose base is *also*
being rebuilt in the same run are skipped there, because without a push they
would build against the previously published base and prove nothing.

## Two-way jump (log ↔ Dockerfile)

**Image → source:** the tag *is* the short commit SHA, so browse
`…/blob/<sha>/public-images/<platform>/<variant>/Dockerfile`. When you hold a
digest rather than a tag, the labels have it:

```sh
skopeo inspect docker://eccr.ecmwf.int/public-ci-images/<platform>-<variant>:<tag> \
  | jq '.Labels | {dockerfile: ."int.ecmwf.ci.dockerfile",
                   revision:   ."org.opencontainers.image.revision"}'
```

From inside a running container: `echo "$CI_INFRASTRUCTURE_BAKED_REF"`.

**Source → image:** `eccr.ecmwf.int/public-ci-images/<platform>-<variant>`,
at the tag `./build-image.sh --print-tag <platform>/<variant>` prints.

**From inside a running container**, where no label is readable:

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

`actions/announce-image` prints them, and takes an `extra` input for anything the
caller knows that the image does not (resolved dep refs, the matrix leg). The
generator emits it into every job, so downstream repos get it without editing a
workflow. It is pure bash and depends on nothing else in this repo — "which image
am I" must not fail for an unrelated reason — and is silent on runners outside
these images.

This is as early as it can be. The runner's own "Set up job" block cannot be
extended, and the job container's `ENTRYPOINT`/`CMD` never run — GitHub starts it
with its own command, which is why every base here ends with `ENTRYPOINT []`. The
image reference and digest *are* already in "Initialize containers"; what this
adds is the readable provenance.

> **Inheritance.** Docker `ENV` is inherited, so an image that `FROM`s one of
> these **must re-declare the whole `ARG`/`ENV` block** or it will announce itself
> as its base, with a Dockerfile URL pointing at the wrong file. Every variant
> here re-declares it, and `smoke-test-runners.yml` asserts a variant reports its
> own name. The same obligation falls on `ecmwf/ci-container-images`, whose
> images `FROM` our base.

## Adding a new image

1. Create `public-images/<platform>/<variant>/Dockerfile`.
2. If it builds on the shared foundation, start with
   `FROM eccr.ecmwf.int/public-ci-images/<platform>-base:latest`.
   Copy the `CI_IMAGE_*` `ARG`/`ENV` block from any existing image — see
   *Self-description*; inheriting the base's is the one way to get it wrong.
3. Open a PR — the workflow discovers the new directory and validates it. On
   merge to `main` it is built and pushed.

No list to edit anywhere: discovery globs `public-images/*/*/Dockerfile`.

## Building by hand

The workflow and a workstation share one script, so both produce identical,
identically-tagged images. The build context is the repo root.

```sh
# validate locally (no push, no credentials needed)
./build-image.sh ubuntu24.04/base

# what would be built right now, and under which tags
./build-image.sh --discover --mode publish
./build-image.sh --print-tag ubuntu24.04/clang18-gfortran13

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
| `CI_PERMISSIONS_APP_CLIENT_ID` / `CI_PERMISSIONS_APP_PRIVATE_KEY` | minting the `actions: write` token that dispatches the private image rebuild |

Reads are anonymous, so the discover job needs no secrets and works on pull
requests from forks.
