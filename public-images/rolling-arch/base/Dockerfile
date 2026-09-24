# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Conventions: public-images/README.md
# The `rolling-` prefix is load-bearing: without it the nightly rebuild stops.
FROM archlinux:latest

# Arch marks its python externally managed (PEP 668).
ENV PIP_BREAK_SYSTEM_PACKAGES=1

#   --disable-sandbox   pacman's landlock/seccomp sandbox cannot be set up in an
#                       unprivileged container; the container is the sandbox.
#   -Syu                a full upgrade: partial upgrades are unsupported.
#   python-pytest       the distro package: Arch's python is current.
#   make, patch         base-devel would drag in gcc.
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

COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN pip install --no-cache-dir "/opt/ci-infrastructure[runtime]" \
 && rm -rf /opt/ci-infrastructure

ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3

# Declared late to keep the pip layer cached. For a rolling image the commit is
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

ENTRYPOINT []
CMD []

WORKDIR /workspace

COPY public-images/announce-image.sh /opt/ci/announce-image.sh
ENV BASH_ENV=/opt/ci/announce-image.sh

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
