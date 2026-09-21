# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-clang18
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.md).
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# No Fortran: gfortran-N Depends on gcc-N, so a clang image offering Fortran also
# ships a GNU C compiler its name does not mention. eccodes, the only leg here
# that wanted it, builds its Fortran interface on the gcc13 legs and on HPC.
#
# libstdc++-N-dev: clang++ compiles C++ against GCC's libstdc++ headers.
RUN apt-get update && apt-get install -y \
    clang-18 \
    clang++-18 \
    clang-tidy-18 \
    libomp-18-dev \
    libstdc++-13-dev \
    libc6-dev \
 && rm -rf /var/lib/apt/lists/*

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
