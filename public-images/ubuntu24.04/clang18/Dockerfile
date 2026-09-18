# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-clang18
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# No Fortran here, deliberately. gfortran-N Depends on gcc-N, so a clang image
# that offers Fortran also ships a GNU C compiler its name does not mention --
# which is the whole thing the name-is-the-toolchain contract exists to stop, and
# what /usr/bin/cc used to fall through to. Of the legs that run in this image
# only eccodes wanted Fortran, and it builds its Fortran interface on the gcc13
# legs and on HPC. If clang-native Fortran is ever wanted, flang belongs in a
# variant of its own (ecbuild already knows the LLVMFlang compiler id).
#
# libstdc++-N-dev: clang++ compiles C++ against GCC's libstdc++ headers, which
# used to arrive with the base's build-essential. It carries no compiler.
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
