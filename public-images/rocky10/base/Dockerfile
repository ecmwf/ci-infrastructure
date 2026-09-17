# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky10-base
# Shared foundation: every other public-images/rocky10/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM rockylinux/rockylinux:10.1

# Rocky 10's system Python 3.12 and CMake 3.31 meet the repository floors. CRB
# supplies Ninja and the static Boost libraries used by the UI variant.
RUN dnf -y install dnf-plugins-core \
 && dnf config-manager --set-enabled crb \
 && dnf -y install \
    sudo \
    git \
    bison \
    flex \
    curl \
    wget \
    ca-certificates \
    gnupg2 \
    cmake \
    ninja-build \
    jq \
    unzip \
    zstd \
    make \
    gcc \
    gcc-c++ \
    redhat-rpm-config \
    python3 \
    python3-devel \
    python3-pip \
    openssl-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN python3 -m pip install --no-cache-dir pytest

# Install GitHub CLI (gh) from its upstream RPM repository.
RUN dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo \
 && dnf -y install gh \
 && dnf clean all && rm -rf /var/cache/dnf

# Install ci-infrastructure from the repository build context. Keep this
# non-editable so the installed package remains after the source tree is removed.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN python3 -m pip install --no-cache-dir "/opt/ci-infrastructure[all]" \
 && rm -rf /opt/ci-infrastructure

ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3

ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

ENTRYPOINT []
CMD []
WORKDIR /workspace

COPY public-images/announce-image.sh /opt/ci/announce-image.sh
ENV BASH_ENV=/opt/ci/announce-image.sh

# Self-description must be re-declared by every descendant image.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
