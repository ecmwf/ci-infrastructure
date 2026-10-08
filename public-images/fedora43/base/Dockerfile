# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Conventions: public-images/README.md
FROM fedora:43

ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN dnf -y install \
    sudo \
    git \
    bison \
    flex \
    perl-FindBin \
    perl-File-Compare \
    curl \
    diffutils \
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
    zlib-devel \
    libcurl-devel \
    ncurses-devel \
    gh \
 && dnf clean all && rm -rf /var/cache/dnf

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
