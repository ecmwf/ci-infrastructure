# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Conventions: public-images/README.md
FROM rockylinux/rockylinux:8

# /usr/bin/python3 is 3.6 and rpm scripts need it, so builds get the python3.12
# package, with pytest from pip in that interpreter (ecflow's tests
# run `<interpreter> -m pytest`). CRB (powertools) carries the variants' -devel
# packages. `dnf update expat` fixes python3.12's pyexpat on aarch64.
RUN dnf -y install dnf-plugins-core \
 && dnf config-manager --set-enabled powertools \
 && dnf -y install \
    sudo \
    git \
    bison \
    flex \
    curl \
    diffutils \
    ncurses-devel \
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
    python3.12 \
    python3.12-devel \
    python3.12-pip \
    openssl-devel \
    zlib-devel \
    libcurl-devel \
 && dnf -y update expat \
 && dnf clean all && rm -rf /var/cache/dnf

RUN python3.12 -m pip install --no-cache-dir pytest

# Rocky 8's cmake 3.26 sits exactly on the floor; pin a modern one.
RUN python3.12 -m pip install --no-cache-dir cmake==3.31.6

# Rocky ships no gh package: use the upstream RPM repo.
RUN dnf -y install 'dnf-command(config-manager)' \
 && dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo \
 && dnf -y install gh \
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
