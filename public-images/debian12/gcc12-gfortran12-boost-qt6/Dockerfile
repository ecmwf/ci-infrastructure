# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian12-gcc12-gfortran12-boost-qt6
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# This variant installs its complete toolchain directly on the shared base. The
# image workflow supports only base -> variant dependency chains.
FROM eccr.ecmwf.int/public-ci-images/debian12-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Debian 12 provides the required Qt 6 components, including Charts for
# ECFLOW_LOGVIEW and Core5Compat for APIs retained from Qt 5.
RUN apt-get update && apt-get install -y \
    gcc-12 \
    g++-12 \
    gfortran-12 \
    libboost-all-dev \
    qt6-base-dev \
    qt6-svg-dev \
    qt6-5compat-dev \
    qt6-charts-dev \
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
