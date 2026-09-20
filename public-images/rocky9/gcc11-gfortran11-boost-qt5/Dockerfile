# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky9-gcc11-gfortran11-boost-qt5
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# This variant installs its complete toolchain directly on the shared base. The
# image workflow supports only base -> variant dependency chains.
FROM eccr.ecmwf.int/public-ci-images/rocky9-base:latest

# Rocky 9 supplies Qt 5. Charts comes from EPEL and is included so ecFlow's
# ECFLOW_LOGVIEW feature is exercised consistently with the other UI images.
RUN dnf -y install epel-release \
 && dnf -y install \
    gcc \
    gcc-c++ \
    gcc-gfortran \
    boost-devel \
    boost-static \
    qt5-qtbase-devel \
    qt5-qtsvg-devel \
    qt5-qtcharts-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-11; \
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
