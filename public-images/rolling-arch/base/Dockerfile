# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Conventions: public-images/README.md
# The `rolling-` prefix is load-bearing: without it the nightly rebuild stops.
FROM archlinux:latest

# Arch marks its python externally managed (PEP 668).
ENV PIP_BREAK_SYSTEM_PACKAGES=1

#   --disable-sandbox   pacman's landlock/seccomp sandbox cannot be set up in an
#                       unprivileged container; the container is the sandbox.
#   -Syu                a full upgrade: partial upgrades are unsupported.
#   python-pytest       the distro package: Arch's python is current.
#   make, patch         base-devel would drag in gcc.
RUN pacman -Syu --noconfirm --disable-sandbox \
    sudo \
    git \
    bison \
    flex \
    curl \
    diffutils \
    procps-ng \
    wget \
    ca-certificates \
    gnupg \
    cmake \
    ninja \
    jq \
    unzip \
    zstd \
    make \
    patch \
    python \
    python-pip \
    python-pytest \
    openssl \
    zlib \
    ncurses \
    github-cli \
 && pacman -Scc --noconfirm

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

# Declared late to keep the pip layer cached. For a rolling image the commit is
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
