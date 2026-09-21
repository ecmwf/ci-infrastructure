# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu22.04-gcc11-gfortran11-boost-qt6
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.md).
#
# This variant installs its complete toolchain directly on the shared base. The
# image workflow supports only base -> variant dependency chains.
FROM eccr.ecmwf.int/public-ci-images/ubuntu22.04-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Jammy names the Qt 6 module development packages after their libraries rather
# than using the qt6-<module>-dev convention found in newer Ubuntu releases.
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

# Self-description must be re-declared by every descendant image.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
