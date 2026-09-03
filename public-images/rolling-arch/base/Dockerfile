# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-arch-base
# Shared foundation: every other public-images/rolling-arch/* image FROMs this.
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# THE `rolling-` PREFIX IS LOAD-BEARING. build-image.sh's is_rolling() matches any
# platform named `rolling` or `rolling-*`, gives those images a date-suffixed tag,
# and images.yml rebuilds them nightly. So the prefix states the guarantee -- this
# tracks upstream rather than pinning a release -- while the suffix names the
# distro delivering it, leaving room for a second rolling platform later. Renaming
# the directory out of that prefix would silently turn the nightly rebuild off.
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
