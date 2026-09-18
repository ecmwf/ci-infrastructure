# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-arch-base
# Shared foundation: every other public-images/rolling-arch/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# THE `rolling-` PREFIX IS LOAD-BEARING: scripts/build_image.py's is_rolling()
# gives such platforms a date-suffixed tag and images.yml rebuilds them nightly.
# Renaming out of that prefix silently turns the nightly rebuild off.
#
# Tracks upstream, so a gcc, cmake or Qt change breaks here on a nightly build
# before it reaches the pinned platforms.
FROM archlinux:latest

# Arch marks its python externally managed (PEP 668), same as ubuntu.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# The rolling counterpart of public-images/ubuntu24.04/base -- see that file for
# why each of these belongs in the BASE rather than a variant. Differences:
#
#   --disable-sandbox   pacman's landlock/seccomp sandbox cannot be set up in an
#                       unprivileged container; the container is the sandbox.
#   -Syu                a full upgrade: partial upgrades are unsupported.
#   github-cli          Arch packages gh; no upstream repo to add.
#   python-pytest       the distro package: Arch's python is current.
#   make, patch         base-devel would drag in gcc, and the toolchain belongs
#                       to the named variant.
RUN pacman -Syu --noconfirm --disable-sandbox \
    sudo \
    git \
    bison \
    flex \
    curl \
    wget \
    ca-certificates \
    gnupg \
    cmake \
    ninja \
    jq \
    unzip \
    zstd \
    make \
    patch \
    python \
    python-pip \
    python-pytest \
    openssl \
    github-cli \
 && pacman -Scc --noconfirm

# Install ci-infrastructure from the build context; see ubuntu24.04/base.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN pip install --no-cache-dir "/opt/ci-infrastructure[all]" \
 && rm -rf /opt/ci-infrastructure

# Advertise the interpreter ci-infrastructure was baked into.
ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3

# Declared late to keep the pip layer cached. For a rolling image the commit is
# only half the story; the tag's date suffix is the other half.
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

ENTRYPOINT []
CMD []

WORKDIR /workspace

COPY public-images/announce-image.sh /opt/ci/announce-image.sh
ENV BASH_ENV=/opt/ci/announce-image.sh

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
