# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-gfortran13
#
# TEMPORARY, superseded by ubuntu24.04/gcc13-gfortran13 — see "Legacy names" in
# IMAGES.md before touching this file.
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# build-essential is what this image used to inherit from the base, and callers
# of this name built against the unversioned gcc/g++/cc it brings.
RUN apt-get update && apt-get install -y \
    build-essential \
    gfortran-13 \
 && rm -rf /var/lib/apt/lists/*

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
