# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian12-base
# Shared foundation: every other public-images/debian12/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM debian:12

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Debian 12 provides Python 3.11, which meets ci-infrastructure's minimum.
# Its CMake 3.25 does not meet the 3.26 floor required by the submodules, so a
# pinned CMake is installed with pip below and takes precedence from /usr/local.
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
    ninja-build \
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

RUN python3 -m pip install --no-cache-dir cmake==3.31.6

# Install GitHub CLI (gh) from its upstream apt repository.
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
