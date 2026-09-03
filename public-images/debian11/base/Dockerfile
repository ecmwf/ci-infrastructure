# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-base
# Shared foundation: every other public-images/debian11/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM debian:11

ENV DEBIAN_FRONTEND=noninteractive

# The debian11 counterpart of public-images/ubuntu24.04/base. That file explains
# why each package belongs in the BASE rather than a variant; this one records
# only where bullseye forces a different answer.
#
# The big one is PYTHON, handled in the next layer: bullseye's python3 is 3.9 and
# its newest packaged interpreter is 3.10, while ci_infrastructure declares
# requires-python >= 3.11 (pyproject.toml). There is no backport -- so unlike
# every other platform here, this image builds its interpreter from source.
#
# python3 / python3-pip are still installed: plenty of tooling shells out to
# `python3`, and it costs little to have the distro one present. It is simply not
# the interpreter CI_INFRASTRUCTURE_PYTHON names.
#
# The libraries after `build-essential` are Python's build dependencies -- the
# set that decides which stdlib modules get built. Missing ones do NOT fail the
# build, they silently produce an interpreter without ssl, or without sqlite3,
# or without zlib, which then fails much later inside pip. They are listed
# explicitly for that reason.
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

# Python 3.11 from source -- see above for why there is no packaged option.
#
# Pinned with a checksum: this is the one thing in any of these images fetched
# from outside a distro archive, so it does not get to be a moving target.
#
# --enable-shared, because ecflow (and anything else using
# find_package(Python3 COMPONENTS Development)) needs libpython3.11.so, which a
# default static build does not produce. ldconfig then makes it findable;
# /usr/local/lib is already on debian's default search path.
#
# `make altinstall`, never `make install`: the latter would overwrite
# /usr/bin/python3 and take apt, dpkg and half the distro's python scripts with
# it. This lands as python3.11 alongside the system 3.9.
#
# No --enable-optimizations: PGO roughly triples the build time of an image
# whose interpreter only ever runs CI glue.
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

# pytest for the interpreter this image actually runs things with. The
# python3-pytest deb is pytest 6.0 built for the 3.9 interpreter, which is not
# the one anything here uses -- and ecflow's py3_u_* tests shell out to
# `<interpreter> -m pytest`, so it has to be importable from python3.11.
RUN python3.11 -m pip install --no-cache-dir pytest

# CMake is the one piece of tooling here that is NOT what this platform exists to
# test: the old compiler, boost and Qt are the point, the build driver is not.
# Bullseye's 3.18 is below the 3.26 our submodules require (libaec). Pin a
# modern one; /usr/local/bin precedes /usr/bin.
RUN python3.11 -m pip install --no-cache-dir cmake==3.31.6

# Install GitHub CLI (gh) -- same upstream apt repo as the ubuntu base; debian
# ships no gh package either.
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
# network -- see public-images/ubuntu24.04/base/Dockerfile for the full
# reasoning, which applies verbatim here.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN python3.11 -m pip install --no-cache-dir /opt/ci-infrastructure \
 && rm -rf /opt/ci-infrastructure

# Advertise the interpreter ci-infrastructure was baked into. The source-built
# 3.11, NOT /usr/bin/python3 (3.9), for the reason at the top of this file.
ENV CI_INFRASTRUCTURE_PYTHON=/usr/local/bin/python3.11

# Record WHICH ci-infrastructure got baked. Declared LAST so a new revision never
# busts the cache of the pip layer above.
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

# Make container ARC/GitHub-Actions compatible
ENTRYPOINT []
CMD []

# Useful default working directory
WORKDIR /workspace

# Announce the image from the first bash step of any job, no workflow step
# required: GitHub runs every step as `bash --noprofile --norc -e -o pipefail`,
# which sources $BASH_ENV. Inherited by descendants on purpose -- this is
# behaviour reading the CI_IMAGE_* below, which each image re-declares itself.
COPY public-images/announce-image.sh /opt/ci/announce-image.sh
ENV BASH_ENV=/opt/ci/announce-image.sh

# Self-description. The same facts build-image.sh writes as OCI labels, baked in
# as environment as well: a label can only be read from OUTSIDE the image
# (registry or daemon), and a job running INSIDE it has neither.
# announce-image.sh and actions/announce-image print these. See IMAGES.md.
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
