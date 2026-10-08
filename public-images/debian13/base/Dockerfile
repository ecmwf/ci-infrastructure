# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Conventions: public-images/README.md
FROM debian:13

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# flex recommends libfl-dev, which pulls PCC and registers it as /usr/bin/cc.
RUN apt-get update && apt-get install -y \
    sudo \
    git \
    bison \
    flex \
    curl \
    diffutils \
    wget \
    ca-certificates \
    gnupg \
    cmake \
    ninja-build \
    jq \
    unzip \
    zstd \
    lsb-release \
    make \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-pytest \
    python-is-python3 \
    libssl-dev \
    zlib1g-dev \
    libcurl4-openssl-dev \
    libncurses-dev \
    build-essential- \
    gcc- \
    g++- \
    pcc- \
 && rm -rf /var/lib/apt/lists/*

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

# uv, pinned to the same version in every base. ci-infrastructure gets its own
# uv-managed CPython and venv under /opt/ci-infrastructure, kept apart from every
# Python a build may use: recipes create theirs in UV_PYTHON_INSTALL_DIR.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy
COPY pyproject.toml LICENSE /tmp/ci-infrastructure/
COPY src /tmp/ci-infrastructure/src
RUN UV_PYTHON_INSTALL_DIR=/opt/ci-infrastructure/python \
    uv venv --no-cache --python 3.12 /opt/ci-infrastructure/venv \
 && uv pip install --no-cache --python /opt/ci-infrastructure/venv/bin/python "/tmp/ci-infrastructure[runtime]" \
 && rm -rf /tmp/ci-infrastructure

ENV CI_INFRASTRUCTURE_PYTHON=/opt/ci-infrastructure/venv/bin/python

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
