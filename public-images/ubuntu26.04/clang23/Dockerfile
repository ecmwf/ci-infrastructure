# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/ubuntu26.04-base:latest

# Ubuntu ships no clang-23; it comes from apt.llvm.org.
RUN curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/apt.llvm.org.gpg \
 && chmod go+r /etc/apt/keyrings/apt.llvm.org.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/apt.llvm.org.gpg] \
    https://apt.llvm.org/resolute/ llvm-toolchain-resolute-23 main" \
    > /etc/apt/sources.list.d/llvm-23.list

# No Fortran: gfortran-N Depends on gcc-N, which the name does not promise.
# libstdc++-N-dev: clang++ compiles against GCC's libstdc++ headers.
RUN apt-get update && apt-get install -y \
    clang-23 \
    clang++-23 \
    clang-tidy-23 \
    libomp-23-dev \
    libstdc++-15-dev \
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
