# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-base
# Shared foundation: every other public-images/ubuntu24.04/* image FROMs this, as
# do the private images in ecmwf/ci-container-images.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# sudo so the same scripts work as root in containers and on hosted runners.
# ctest (with cmake) and pytest are used undeclared by every repo; pytest is the
# distro package because ecflow's tests run `/usr/bin/python3 -m pytest`.
# bison + flex: without them eckit's build stays GREEN while silently dropping
# eckit::sql and eckit::cmd. zstd unpacks HPC install archives.
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

# Install ci-infrastructure from the build context, so an image tagged <sha>
# contains <sha> (src/, pyproject.toml and LICENSE are tag paths). LICENSE must be
# copied: setuptools silently builds a wheel without a missing license file. Not
# editable: ensure-infrastructure-present hashes the installed *.py files.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN pip install "/opt/ci-infrastructure[all]" \
 && rm -rf /opt/ci-infrastructure

# ensure-infrastructure-present reuses this interpreter instead of a venv. It
# lives outside _work, which ARC's Kubernetes mode overlays per job.
ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3

# Which ci-infrastructure commit got baked, readable inside a job (named in
# ensure-infrastructure-present's mismatch error). Declared late to keep the pip
# layer cached.
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

# Make container ARC/GitHub-Actions compatible
ENTRYPOINT []
CMD []

WORKDIR /workspace

# Announce the image from the first bash step of any job: GitHub's step shell
# sources $BASH_ENV. Inherited by every image built on this base.
COPY public-images/announce-image.sh /opt/ci/announce-image.sh
ENV BASH_ENV=/opt/ci/announce-image.sh

# Self-description: the OCI label facts as env, since a job inside the image
# cannot read its labels (see IMAGES.md). Declared last, and repeated in EVERY
# image: an inherited copy would announce the base's name.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
