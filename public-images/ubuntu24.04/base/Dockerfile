# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-base
# Shared foundation: every other public-images/ubuntu24.04/* image FROMs this, as
# do the private images in ecmwf/ci-container-images.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Base system dependencies
#
# We intentionally install sudo so the same scripts work both:
# - inside CI containers (where we are typically root already)
# - on GitHub-hosted runners (where sudo is required)
#
# Two test runners belong here rather than in a variant, because every repo in
# the stack runs its suite from CI and both are consumed as `<runner> ...` with
# nothing declaring them:
#   ctest   comes with the `cmake` package below -- no separate install needed.
#   pytest  as the DISTRO package, not pip: ecflow's py3_u_* tests shell out to
#           `/usr/bin/python3 -m pytest`, so it has to be importable from the
#           system interpreter (dist-packages), which is exactly where apt puts
#           it. Without it those ~52 tests fail instantly with "No module named
#           pytest" rather than actually running.
#
# bison + flex belong in the BASE, not a toolchain variant. eckit's
# ecbuild_generate_yy() builds eckit::sql and eckit::cmd from .y/.l sources, and
# both features are declared with `CONDITION LEXYACC_FOUND` -- so without these
# two packages ecbuild_add_option takes its non-fatal branch and the build is
# GREEN while silently shipping neither library and skipping the whole tests/sql
# suite. stack-deps needs them too, for libpq's generated parser. (perl, which
# ecbuild_generate_yy also requires, arrives with build-essential.)
#
# zstd is what HPC install archives are compressed with (CI_INSTALL_ARCHIVE);
# fetch_install unpacks them here, so it is needed wherever an HPC job runs.
RUN apt-get update && apt-get install -y \
    sudo \
    git \
    bison \
    flex \
    curl \
    wget \
    ca-certificates \
    gnupg \
    cmake \
    jq \
    unzip \
    zstd \
    lsb-release \
    software-properties-common \
    build-essential \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-pytest \
    python-is-python3 \
    libssl-dev \
 && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh)
RUN mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) \
    signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] \
    https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y gh \
 && rm -rf /var/lib/apt/lists/*

# Install ci-infrastructure from the build context (the repo root), not over the
# network. The source lives in THIS repo, so the image and the commit that built
# it agree by construction: no pin file, no `git+https` fetch of our own sources,
# and an image tagged <sha> provably contains <sha>. build-image.sh makes that
# true by putting src/, pyproject.toml and LICENSE in this image's tag paths, so
# any change to what gets installed retags the base and cascades to every variant.
#
# LICENSE must be in the context, and its absence is SILENT. pyproject.toml
# declares `license-files = ["LICENSE"]`; setuptools (checked against 84.0.0)
# does not error when that pattern matches nothing -- it just builds a wheel with
# no license file in it. For an Apache-2.0 REUSE-compliant project that is a
# compliance defect that no build log would ever mention, so LICENSE is copied
# here and is one of the image's tag paths.
#
# The install is deliberately NOT editable: actions/ensure-infrastructure-present
# locates the package via ci_infrastructure.__file__ and hashes its *.py files, so
# it needs a real copy in site-packages that outlives the `rm -rf` below. That
# `rm -rf` reclaims the layer's working copy only; the COPY still shows up in
# `docker history`, which is fine — this is a public repo.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN pip install /opt/ci-infrastructure \
 && rm -rf /opt/ci-infrastructure

# Advertise the interpreter ci-infrastructure was baked into, so
# ensure-infrastructure-present reuses it instead of building a fresh venv per
# job. It must live outside _work: ARC's Kubernetes mode overlays the runner's
# _work tree (where RUNNER_TOOL_CACHE lives) with an ephemeral per-job volume,
# so a baked tool-cache venv would be shadowed — the system interpreter is not.
ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3

# Record WHICH ci-infrastructure got baked, readable from inside a running job.
# ensure-infrastructure-present reports this in its version-mismatch error so the
# message names the commit to bump to, and smoke-test-runners.yml asserts it is
# present. Same value as the image tag and as org.opencontainers.image.revision.
#
# Declared LAST so a new revision never busts the cache of the pip layer above.
# The ARG name is generic because build-image.sh passes it to every image; the
# repo-specific name stays here, in the Dockerfile, where it belongs.
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

# Make container ARC/GitHub-Actions compatible
ENTRYPOINT []
CMD []

# Useful default working directory
WORKDIR /workspace

# Self-description. The same facts build-image.sh writes as OCI labels, baked in
# as environment as well: a label can only be read from OUTSIDE the image
# (registry or daemon), and a job running INSIDE it has neither.
# actions/announce-image prints these. See IMAGES.md.
#
# Declared LAST so a new value never busts the cache of the layers above, and
# repeated in EVERY image rather than inherited: ENV is inherited by descendants,
# so a variant that did not re-declare these would announce itself as its base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
