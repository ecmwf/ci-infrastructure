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

### Label set

`org.opencontainers.image.{source,revision,created,title,description}`, plus
`.base.name` on dependents, plus `int.ecmwf.ci.dockerfile`. The private repo
writes the same set by hand (and adds `.base.digest` and `int.ecmwf.ci.build.run`,
because its base is external and its content is not a pure function of git).

## Adding a new image

1. Create `public-images/<platform>/<variant>/Dockerfile`.
2. If it builds on the shared foundation, start with
   `FROM eccr.ecmwf.int/public-ci-images/<platform>-base:latest`.
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
