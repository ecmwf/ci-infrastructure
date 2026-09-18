# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-clang18-gfortran13
#
# TEMPORARY, superseded by ubuntu24.04/clang18 — see "Legacy names" in IMAGES.md
# before touching this file. No new image should pair clang with gfortran.
#
# It cannot be an alias of clang18: eccodes/develop runs its g++-13 and its
# clang++-18 leg in this one image, and builds Fortran on both.
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# build-essential is what this image used to inherit from the base.
RUN apt-get update && apt-get install -y \
    build-essential \
    gfortran-13 \
    clang-18 \
    clang++-18 \
    clang-tidy-18 \
    libomp-18-dev \
 && rm -rf /var/lib/apt/lists/*

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
