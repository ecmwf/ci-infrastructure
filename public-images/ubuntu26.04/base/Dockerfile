# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu26.04-base
# Shared foundation: every other public-images/ubuntu26.04/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.rst).
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Ubuntu 26.04 provides Python 3.14 and CMake 4.2, so the complete base
# tools can come from the distribution repositories.
# Keep compilers in named variants. The negative package selections prevent
# python3-dev/pip recommendations from pulling build-essential back in.
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
    make \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-pytest \
    python-is-python3 \
    libssl-dev \
    build-essential- \
    gcc- \
    g++- \
 && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (gh) from its upstream apt repository rather than the older
# package in the Ubuntu archive.
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
