# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-gfortran10
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/debian11-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Toolchain layered on the shared base, which already provides sudo, git, curl,
# cmake, build-essential, gh, python3.11, etc.
#
# 10 rather than the 12/13 the ubuntu variants carry: bullseye's newest gcc is
# 10, and that is the point of having this platform in the matrix at all.
RUN apt-get update && apt-get install -y \
    gcc-10 \
    g++-10 \
    gfortran-10 \
 && rm -rf /var/lib/apt/lists/*

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
