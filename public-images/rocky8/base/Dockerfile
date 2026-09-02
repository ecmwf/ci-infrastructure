# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-base
# Shared foundation: every other public-images/rocky8/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM rockylinux/rockylinux:8

# The rocky8 counterpart of public-images/ubuntu24.04/base. That file explains
# why each package belongs in the BASE rather than a variant (bison/flex for
# eckit's generated parsers, zstd for the HPC install archives, and so on); this
# one only records where rocky 8 forces a different answer.
#
# PYTHON. /usr/bin/python3 here is 3.6, and ci_infrastructure declares
# requires-python >= 3.11 (pyproject.toml), so the pip install below cannot use
# it. python3.12 is a first-class dnf package on rocky 8, so we install that and
# point CI_INFRASTRUCTURE_PYTHON at it -- unlike ubuntu, where the system
# interpreter is already new enough. Nothing is symlinked over /usr/bin/python3:
# rpm scripts across the distro are built against 3.6 and hijacking it breaks
# dnf itself.
#
# PYTEST. From pip, not the python3-pytest RPM. That RPM is pytest 3.4 for the
# 3.6 interpreter, which is not the interpreter anything here runs. It has to be
# importable from the SAME python3.12 that CI_INFRASTRUCTURE_PYTHON names,
# because ecflow's py3_u_* tests shell out to `<interpreter> -m pytest`. On
# ubuntu the distro package lands in the right interpreter and is used as-is.
#
# CRB (PowerTools) is enabled because the -devel packages the toolchain variants
# need -- qt5-qtsvg-devel among them -- live there rather than in AppStream.
#
# The `dnf -y update expat` is not cosmetic. On aarch64 the python3.12 RPM is
# linked against a newer libexpat than the one rocky 8 ships installed, so
# `python3.12 -m pip` dies with an undefined XML_SetBillionLaughsAttack...
# symbol out of pyexpat. amd64 -- what CI builds -- is unaffected, but
# build-image.sh is meant to be runnable on a workstation, and this is one line.
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

# pytest for the interpreter this image actually runs things with -- see above.
RUN python3.12 -m pip install --no-cache-dir pytest

# Install GitHub CLI (gh). Rocky ships no gh package, so this is the upstream
# RPM repo -- the counterpart of the apt repo the ubuntu base adds.
RUN dnf -y install 'dnf-command(config-manager)' \
 && dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo \
 && dnf -y install gh \
 && dnf clean all && rm -rf /var/cache/dnf

# Install ci-infrastructure from the build context (the repo root), not over the
# network -- see public-images/ubuntu24.04/base/Dockerfile for the full
# reasoning, which applies verbatim here (LICENSE must be in the context, the
# install is deliberately not editable, and the `rm -rf` reclaims the layer's
# working copy only).
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN python3.12 -m pip install --no-cache-dir /opt/ci-infrastructure \
 && rm -rf /opt/ci-infrastructure

# Advertise the interpreter ci-infrastructure was baked into, so
# ensure-infrastructure-present reuses it instead of building a fresh venv per
# job. python3.12, NOT /usr/bin/python3, for the reason at the top of this file.
ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3.12

# Record WHICH ci-infrastructure got baked. Declared LAST so a new revision never
# busts the cache of the pip layer above.
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

# Make container ARC/GitHub-Actions compatible
ENTRYPOINT []
CMD []

# Useful default working directory
WORKDIR /workspace
