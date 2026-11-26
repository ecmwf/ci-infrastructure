# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/ubuntu26.04-base:latest

# gcc15-gfortran15, plus what release builds link and package with (README "Release images").
# dpkg-dev and file are what CPack's DEB generator runs (dpkg-shlibdeps reads the binaries).
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y \
    gcc-15 \
    g++-15 \
    gfortran-15 \
    dpkg-dev \
    file \
    libaec-dev \
    libbz2-dev \
    liblz4-dev \
    libblas-dev \
    liblapack-dev \
    libeigen3-dev \
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
