<!--
SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)

SPDX-License-Identifier: Apache-2.0
-->

# Public CI images

The images CI jobs run in. They are published to `eccr.ecmwf.int/public-ci-images` (world-readable).
[`images.yml`](../.github/workflows/images.yml) builds, tests and pushes them through [`build-image.sh`](../build-image.sh).
The private images live in [ecmwf/ci-container-images](https://github.com/ecmwf/ci-container-images) and follow the same rules.

## Naming

`public-images/<platform>/<variant>/Dockerfile` is published as `public-ci-images/<platform>-<variant>`.

- `base` has system packages, cmake, Python and the baked `ci_infrastructure` package. It has **no compiler**.
- Every variant `FROM`s its platform's `base` directly. Variants never build on each other.
- The name is the whole toolchain. `gcc<N>`, `clang<N>` and `gfortran<N>` each promise that compiler with working OpenMP. Nothing else is installed.
- A `rolling-*` platform tracks upstream and is rebuilt nightly.

[`verify-image.sh`](verify-image.sh) checks this contract.

| Platform | gcc | Qt | boost | cmake | Python |
|---|---|---|---|---|---|
| `ubuntu22.04` | 11 | 6 | 1.74 | 3.31.6 | 3.11 (from source) |
| `ubuntu24.04` | 12, 13 | 6 | 1.83 | 3.28 | 3.12 |
| `ubuntu26.04` | 15 | 6 | 1.90 | 4.2 | 3.14 |
| `rocky8` | 8.5 | **5** | 1.66 | 3.26 | 3.12 |
| `rocky9` | 11 | **5** | 1.75 | 3.31 | 3.12 |
| `rocky10` | 14 | 6 | 1.83 | 3.31 | 3.12 |
| `debian11` | 10 | **5** | 1.74 | 3.18 | 3.11 (from source) |
| `debian12` | 12 | 6 | 1.74 | 3.31.6 | 3.11 (from source) |
| `debian13` | 14 | 6 | 1.83 | 3.31 | 3.13 |
| `fedora43` | 15 | 6 | 1.83 | 3.31 | 3.14 |
| `fedora44` | 16 | 6 | 1.90 | 4.3 | 3.14 |
| `rolling-arch` | newest | 6 | newest | newest | newest |

## Tags and rebuilds

- The tag is the short SHA of the last commit touching the image's inputs. The inputs are its directory, its base's directory, `src`, `pyproject.toml` and `LICENSE`.
- Rolling images add the date: `<sha>-<YYYYMMDD>`.
- **An image is rebuilt when its tag is not in the registry.** That is the only rule. Don't add `paths:` filters, diffs against a base or lists of images.
- Each job builds the image, runs `build-image.sh --test`, and pushes that same image. Pushes happen on `main` only.
- A push to `main` also moves `:latest`.

## Self-description

The base bakes `CI_IMAGE_NAME`, `CI_IMAGE_TAG`, `CI_IMAGE_CREATED` and `CI_IMAGE_DOCKERFILE_URL` into the environment.
Every job prints them through `BASH_ENV` or the `announce-image` action.

Docker `ENV` is inherited. **Every Dockerfile must re-declare the whole `CI_IMAGE_*` block**, or it announces itself as its base.
Copy the block from an existing image.

The base also sets `CI_INFRASTRUCTURE_PYTHON` and `CI_INFRASTRUCTURE_BAKED_REF`.
`ensure-infrastructure-present` uses them to reuse the baked package, or to reinstall it when it is stale.

## Adding, removing, pruning

- **Add:** create `<platform>/<variant>/Dockerfile` with `FROM eccr.ecmwf.int/public-ci-images/<platform>-base:latest`, and open a PR. Discovery is a glob, so there is no list to edit.
- **Remove:** deleting the directory stops the builds. The repository stays in Harbor and keeps serving `:latest`. Run the **Registry cleanup** workflow with task `orphans` to list or delete it.
- **Quota:** the project has 100 GiB. Registry cleanup prunes nightly and keeps the two newest versions and `latest`. A push failing with *"exceed the configured upper limit"* means the quota is full. Dispatch the cleanup with `delete`, then re-run the build.

## Building by hand

The build context is the repo root. `docker buildx` is required.

```sh
./build-image.sh ubuntu24.04/base                  # build
./build-image.sh --test ubuntu24.04/base           # build and verify
./build-image.sh --print-tag ubuntu24.04/clang18   # the tag it would get
./build-image.sh --discover --mode publish         # what CI would build now
./build-image.sh ubuntu24.04/base --push           # needs PUBLIC_ECCR_ROBOT_NAME/_TOKEN
```

## Secrets

| Secret | Used for |
|---|---|
| `PUBLIC_ECCR_ROBOT_NAME` / `_TOKEN` | pushing |
| `PUBLIC_ECCR_CLEANUP_ROBOT_NAME` / `_TOKEN` | registry cleanup (delete rights, no push) |
| `CI_PERMISSIONS_APP_CLIENT_ID` / `_PRIVATE_KEY` | dispatching the private image rebuild |
