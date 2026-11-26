# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/rocky9-base:latest

# gcc11-gfortran11, plus what release builds link and package with (README "Release images").
# CRB (on in the base) has BLAS/LAPACK and Eigen; EPEL has libaec.
RUN dnf -y install epel-release \
 && dnf -y install gcc gcc-c++ gcc-gfortran \
    rpm-build \
    libaec-devel \
    bzip2-devel \
    lz4-devel \
    blas-devel \
    lapack-devel \
    eigen3-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-11; \
    done

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
