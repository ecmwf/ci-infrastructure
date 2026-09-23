# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/fedora43-base
# Shared foundation: every other public-images/fedora43/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.rst).
FROM fedora:43

ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Fedora 43 provides Python 3.14, CMake 3.31, and GitHub CLI directly from its
# repositories, so the shared base tools use distribution packages.
RUN dnf -y install \
    sudo \
    git \
    bison \
    flex \
    perl-FindBin \
    perl-File-Compare \
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
    redhat-rpm-config \
    python3 \
    python3-devel \
    python3-pip \
    python3-pytest \
    openssl-devel \
    gh \
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
