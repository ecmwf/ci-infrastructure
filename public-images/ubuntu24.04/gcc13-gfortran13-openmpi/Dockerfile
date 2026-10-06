# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# No clang variant: libopenmpi-dev Depends on gcc-13 and gfortran-13.
RUN apt-get update && apt-get install -y \
    gcc-13 \
    g++-13 \
    gfortran-13 \
    libopenmpi-dev \
    openmpi-bin \
 && rm -rf /var/lib/apt/lists/*

# Jobs run as root in containers with few cores; ctest's MPI tests ask for more ranks.
ENV OMPI_ALLOW_RUN_AS_ROOT=1 \
    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 \
    OMPI_MCA_rmaps_base_oversubscribe=1

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
