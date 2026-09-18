# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-gfortran10
#
# TEMPORARY, superseded by debian11/gcc10-gfortran10 — see "Legacy names" in
# IMAGES.md before touching this file.
FROM eccr.ecmwf.int/public-ci-images/debian11-base:latest

# 10 rather than the 12/13 the ubuntu variants carry: bullseye's newest gcc is
# 10, and that is the point of having this platform in the matrix at all.
# build-essential is what this image used to inherit from the base.
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc-10 \
    g++-10 \
    gfortran-10 \
 && rm -rf /var/lib/apt/lists/*

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
