# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/fedora43-base:latest

# gcc15-gfortran15, plus what release builds link and package with (README "Release images").
# All in Fedora itself, except libaec (below).
RUN dnf -y install gcc gcc-c++ gcc-gfortran \
    rpm-build \
    bzip2-devel \
    lz4-devel \
    blas-devel \
    lapack-devel \
    eigen3-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-15; \
    done

# libaec from source: Fedora 43's 1.1.4 has a CMake config that needs a libaec.a it does not ship
COPY public-images/build-libaec.sh /tmp/
RUN sh /tmp/build-libaec.sh gcc-15 && rm /tmp/build-libaec.sh

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
