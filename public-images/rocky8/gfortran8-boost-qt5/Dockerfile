# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-gfortran8-boost-qt5
FROM eccr.ecmwf.int/public-ci-images/rocky8-base:latest

# Rocky 8's own gcc 8.5 (see rocky8/gfortran8); the base has gcc and gcc-c++.
#
# Qt5: rocky 8 has no Qt6, and ecflow accepts either. Qt5 counterpart of the
# ubuntu24.04-gfortran13-boost-qt6 set:
#   qt5-qtbase-devel    Widgets, Gui, Network   (required components)
#   qt5-qtsvg-devel     Svg                     (required component)
#   qt5-qtcharts-devel  Charts                  (optional; enables ECFLOW_LOGVIEW)
#
# boost-devel is 1.66, exactly ecflow's ECFLOW_BOOST_VERSION floor, so this image
# keeps that floor honest. boost-static provides libboost_test_exec_monitor.a,
# which boost builds static-only and ecflow needs; everything else stays shared.
# EPEL only for qt5-qtcharts-devel, so ECFLOW_LOGVIEW matches the ubuntu image.
RUN dnf -y install epel-release \
 && dnf -y install \
    gcc-gfortran \
    boost-devel \
    boost-static \
    qt5-qtbase-devel \
    qt5-qtsvg-devel \
    qt5-qtcharts-devel \
 && dnf clean all && rm -rf /var/cache/dnf

# <tool>-8 names, as in rocky8/gfortran8/Dockerfile.
RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-8; \
    done

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
