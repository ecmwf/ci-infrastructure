# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# No Fortran: gfortran-N Depends on gcc-N, which the name does not promise.
# libstdc++-N-dev: clang++ compiles against GCC's libstdc++ headers.
RUN apt-get update && apt-get install -y \
    clang-18 \
    clang++-18 \
    clang-tidy-18 \
    libomp-18-dev \
    libstdc++-13-dev \
    libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
