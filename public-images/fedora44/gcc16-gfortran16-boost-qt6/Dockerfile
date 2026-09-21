# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/fedora44-gcc16-gfortran16-boost-qt6
# Built/pushed by .github/workflows/images.yml (see docs/howto/images.md).
#
# This variant installs its complete toolchain directly on the shared base. The
# image workflow supports only base -> variant dependency chains.
FROM eccr.ecmwf.int/public-ci-images/fedora44-base:latest

# Fedora 44 provides the complete Boost and Qt 6 stack in its own repositories,
# including Charts for ECFLOW_LOGVIEW and Core5Compat for retained Qt 5 APIs.
RUN dnf -y install \
    gcc \
    gcc-c++ \
    gcc-gfortran \
    boost-devel \
    boost-static \
    qt6-qtbase-devel \
    qt6-qtsvg-devel \
    qt6-qt5compat-devel \
    qt6-qtcharts-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-16; \
    done

# Self-description must be re-declared by every descendant image.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
