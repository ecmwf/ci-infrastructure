# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/ubuntu22.04-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Jammy names Qt 6 module packages libqt6<module>6-dev, not qt6-<module>-dev.
RUN apt-get update && apt-get install -y \
    gcc-11 \
    g++-11 \
    gfortran-11 \
    libboost-all-dev \
    qt6-base-dev \
    libqt6svg6-dev \
    libqt6core5compat6-dev \
    libqt6charts6-dev \
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
