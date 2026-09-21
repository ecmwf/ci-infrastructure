# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-base
# Shared foundation: every other public-images/debian11/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.md).
FROM debian:11

ENV DEBIAN_FRONTEND=noninteractive

# Bullseye is end of life and its security archive is being emptied, so install
# from a snapshot.debian.org instant (main, updates and security consistent) with
# Check-Valid-Until off, as snapshot Release files are old. This freezes the
# packages; the real fix is retiring debian11.
ARG DEBIAN_SNAPSHOT=20260815T000000Z
RUN printf '%s\n' \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bullseye main" \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bullseye-updates main" \
      "deb http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bullseye-security main" \
      > /etc/apt/sources.list \
 && echo 'Acquire::Check-Valid-Until "false";' \
      > /etc/apt/apt.conf.d/99no-check-valid-until

# The debian11 counterpart of ubuntu24.04/base; only bullseye-specific answers
# here. Bullseye tops out at python 3.10, so the next layer builds 3.11; the
# libraries after build-essential are its build deps, and a missing one silently
# drops a stdlib module (ssl, sqlite3, zlib) rather than failing the build.
#
# This base cannot simply omit the toolchain -- it compiles CPython -- so
# build-essential is installed here and purged again below. The -dev libraries
# stay: headers, not compilers.
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

# Python 3.11 from source, checksum-pinned. --enable-shared for libpython3.11.so
# (find_package(Python3 Development)); altinstall so /usr/bin/python3 stays the
# distro's; no PGO, which triples build time for CI glue.
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

# pytest in python3.11: ecflow's tests run `<interpreter> -m pytest`.
RUN python3.11 -m pip install --no-cache-dir pytest

# Bullseye's cmake 3.18 is below our 3.26 floor; pin a modern one
# (/usr/local/bin precedes /usr/bin).
RUN python3.11 -m pip install --no-cache-dir cmake==3.31.6

# GitHub CLI from the upstream apt repo, as in the ubuntu base.
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

# Install ci-infrastructure from the build context; see ubuntu24.04/base.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN python3.11 -m pip install --no-cache-dir "/opt/ci-infrastructure[runtime]" \
 && rm -rf /opt/ci-infrastructure

# Last step that may need to compile. Explicit names rather than autoremove,
# which would also take libraries the -dev packages and python3.11 still need.
RUN apt-get purge -y build-essential gcc g++ cpp gcc-10 g++-10 cpp-10 \
 && rm -rf /var/lib/apt/lists/* \
 && for b in cc c++ gcc g++; do ! command -v "$b" >/dev/null || exit 1; done

# The source-built 3.11, not /usr/bin/python3 (3.9).
ENV CI_INFRASTRUCTURE_PYTHON=/usr/local/bin/python3.11

# Declared late to keep the pip layer cached.
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
