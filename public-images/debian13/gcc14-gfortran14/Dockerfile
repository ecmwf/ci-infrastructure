# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian13-gcc14-gfortran14
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.md).
FROM eccr.ecmwf.int/public-ci-images/debian13-base:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Trixie's native compiler generation, exposed under the versioned names used by
# downstream manifests.
RUN apt-get update && apt-get install -y \
    gcc-14 \
    g++-14 \
    gfortran-14 \
 && rm -rf /var/lib/apt/lists/*

# Self-description must be re-declared by every descendant image.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
