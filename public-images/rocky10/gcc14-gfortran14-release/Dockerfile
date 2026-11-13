# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/rocky10-base:latest

# gcc14-gfortran14, plus what release builds link and package with (README "Release images").
# CRB (on in the base) has BLAS/LAPACK and Eigen.
RUN dnf -y install gcc gcc-c++ gcc-gfortran \
    rpm-build \
    bzip2-devel \
    lz4-devel \
    blas-devel \
    lapack-devel \
    eigen3-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-14; \
    done

# libaec from source: Rocky 10's (EPEL) is 1.1.3, below eckit's 1.1.4
COPY public-images/build-libaec.sh /tmp/
RUN sh /tmp/build-libaec.sh gcc-14 && rm /tmp/build-libaec.sh

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
