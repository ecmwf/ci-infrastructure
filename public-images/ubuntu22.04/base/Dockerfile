# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu22.04-base
# Shared foundation: every other public-images/ubuntu22.04/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.md).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Jammy's system Python 3.10 is below ci-infrastructure's 3.11 floor. Keep it for
# distro tooling and install a pinned Python 3.11 alongside it below.
# build-essential is needed for CPython, then purged below; make and headers stay.
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
    build-essential \
    python3 \
    python3-pip \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    libncurses5-dev \
    libffi-dev \
    liblzma-dev \
    uuid-dev \
    libgdbm-dev \
 && rm -rf /var/lib/apt/lists/*

# Jammy only packages a Python 3.11 release candidate. Build a stable,
# checksummed interpreter instead and leave /usr/bin/python3 untouched.
ARG PYTHON_VERSION=3.11.16
ARG PYTHON_SHA256=6c0bd76ab0ec7d94ed400b1497f01ac6c7751c8822615ee0855a3eb2d893ea76
RUN cd /tmp \
 && curl -fsSLO "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz" \
 && echo "${PYTHON_SHA256}  Python-${PYTHON_VERSION}.tgz" | sha256sum -c - \
 && tar -xf "Python-${PYTHON_VERSION}.tgz" \
 && cd "Python-${PYTHON_VERSION}" \
 && ./configure --prefix=/usr/local --enable-shared --with-ensurepip=install \
 && make -j"$(nproc)" \
 && make altinstall \
 && ldconfig \
 && cd /tmp && rm -rf "Python-${PYTHON_VERSION}" "Python-${PYTHON_VERSION}.tgz"

RUN python3.11 -m pip install --no-cache-dir pytest cmake==3.31.6

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

COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN python3.11 -m pip install --no-cache-dir "/opt/ci-infrastructure[all]" \
 && rm -rf /opt/ci-infrastructure

# Last step that may need to compile. Match debian11/base: explicit removal
# keeps the libraries needed by the headers and source-built Python installed.
RUN apt-get purge -y build-essential gcc g++ cpp gcc-11 g++-11 cpp-11 \
 && rm -rf /var/lib/apt/lists/* \
 && for b in cc c++ gcc g++; do ! command -v "$b" >/dev/null || exit 1; done

ENV CI_INFRASTRUCTURE_PYTHON=/usr/local/bin/python3.11

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
