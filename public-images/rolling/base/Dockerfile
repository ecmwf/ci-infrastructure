# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-base
# Shared foundation: every other public-images/rolling/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# THE PLATFORM IS CALLED `rolling`, NOT `arch`, AND THAT NAME IS LOAD-BEARING.
# build-image.sh gives images under public-images/rolling/ a date-suffixed tag
# and images.yml rebuilds them on a nightly schedule. Arch Linux is the current
# implementation of "newest of everything"; the name describes the guarantee, and
# renaming the directory would silently turn the nightly rebuild off.
#
# What it is FOR: everything else in public-images/ pins a distro release, so the
# stack only meets a new gcc, cmake or Qt when someone bumps an image. This one
# tracks upstream continuously, so a compiler or CMake change that will land in
# the pinned platforms in a year breaks HERE first, on a nightly build nobody is
# waiting on, instead of in a release.
FROM archlinux:latest

# Arch marks its python externally managed (PEP 668), same as ubuntu.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# The rolling counterpart of public-images/ubuntu24.04/base -- see that file for
# why each of these belongs in the BASE rather than a variant. Differences:
#
#   --disable-sandbox   pacman 7 sandboxes its own downloads with landlock and
#                       seccomp, which it cannot set up inside an unprivileged
#                       container: it dies with "error restricting syscalls via
#                       seccomp: 22". The sandbox is redundant here anyway --
#                       the container IS the sandbox -- and without this flag the
#                       image cannot be built at all.
#   -Syu                a full upgrade, not just -S. On a rolling distro a
#                       partial upgrade is unsupported and produces exactly the
#                       broken-library-version failures this image exists to
#                       find honestly.
#   github-cli          Arch packages gh, so unlike every other platform here
#                       there is no upstream repo to add.
#   python-pytest       the distro package, importable from /usr/bin/python3 --
#                       Arch's python is current, so this needs none of the
#                       contortions rocky8 and debian11 need.
#   base-devel          the gcc/make/binutils group; the equivalent of debian's
#                       build-essential.
RUN pacman -Syu --noconfirm --disable-sandbox \
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
    base-devel \
    python \
    python-pip \
    python-pytest \
    openssl \
    github-cli \
 && pacman -Scc --noconfirm

# Install ci-infrastructure from the build context (the repo root), not over the
# network -- see public-images/ubuntu24.04/base/Dockerfile for the full
# reasoning, which applies verbatim here.
COPY pyproject.toml LICENSE /opt/ci-infrastructure/
COPY src /opt/ci-infrastructure/src
RUN pip install --no-cache-dir /opt/ci-infrastructure \
 && rm -rf /opt/ci-infrastructure

# Advertise the interpreter ci-infrastructure was baked into.
ENV CI_INFRASTRUCTURE_PYTHON=/usr/bin/python3

# Record WHICH ci-infrastructure got baked. Declared LAST so a new revision never
# busts the cache of the pip layer above.
#
# Note this records the COMMIT, which for a rolling image is only half the story:
# two rolling-base images built a month apart from the same commit hold different
# compilers. That is what the date suffix in the image tag is for.
ARG SOURCE_REVISION=""
ENV CI_INFRASTRUCTURE_BAKED_REF=${SOURCE_REVISION}

# Make container ARC/GitHub-Actions compatible
ENTRYPOINT []
CMD []

# Useful default working directory
WORKDIR /workspace
