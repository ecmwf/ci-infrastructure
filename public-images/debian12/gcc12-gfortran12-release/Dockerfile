# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/debian12-base:latest

# gcc12-gfortran12, plus what release builds link and package with (README "Release images").
# dpkg-dev and file are what CPack's DEB generator runs (dpkg-shlibdeps reads the binaries).
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    gcc-12 \
    g++-12 \
    gfortran-12 \
    dpkg-dev \
    file \
    libbz2-dev \
    liblz4-dev \
    libblas-dev \
    liblapack-dev \
    libeigen3-dev \
 && rm -rf /var/lib/apt/lists/*

# libaec from source: Debian 12's is 1.0.6, below eckit's 1.1.4
COPY public-images/build-libaec.sh /tmp/
RUN sh /tmp/build-libaec.sh gcc-12 && rm /tmp/build-libaec.sh

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
