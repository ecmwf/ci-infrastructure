# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-base
# Shared foundation: every other public-images/rocky8/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM rockylinux/rockylinux:8

# The rocky8 counterpart of ubuntu24.04/base; only rocky-specific answers here.
#
# /usr/bin/python3 is 3.6 and rpm scripts need it, so ci-infrastructure uses the
# python3.12 package, with pytest from pip in that interpreter (ecflow's tests
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
    wget \
    ca-certificates \
    gnupg2 \
    cmake \
    ninja-build \
    jq \
    unzip \
    zstd \
    make \
    gcc \
    gcc-c++ \
    redhat-rpm-config \
    python3.12 \
    python3.12-devel \
    python3.12-pip \
    openssl-devel \
 && dnf -y update expat \
 && dnf clean all && rm -rf /var/cache/dnf

RUN python3.12 -m pip install --no-cache-dir pytest

# Rocky 8's cmake 3.26 sits exactly on our floor; pin a modern one
# (/usr/local/bin precedes /usr/bin).
RUN python3.12 -m pip install --no-cache-dir cmake==3.31.6

# Rocky ships no gh package: use the upstream RPM repo.
RUN dnf -y install 'dnf-command(config-manager)' \
 && dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo \
 && dnf -y install gh \
 && dnf clean all && rm -rf /var/cache/dnf

# Install ci-infrastructure from the build context; see ubuntu24.04/base.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN python3.12 -m pip install --no-cache-dir "/opt/ci-infrastructure[all]" \
 && rm -rf /opt/ci-infrastructure

# python3.12, not /usr/bin/python3 (3.6).
ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3.12

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
