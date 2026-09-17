# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-arch-gfortran-boost-qt6
FROM eccr.ecmwf.int/public-ci-images/rolling-arch-base:latest

# Qt6 here, matching ubuntu24.04-gfortran13-boost-qt6 rather than the Qt5 the
# rocky8 and debian11 variants are stuck with:
#   qt6-base      Widgets, Gui, Network   (required components)
#   qt6-svg       Svg                     (required component)
#   qt6-5compat   Core5Compat             (required component)
#   qt6-charts    Charts                  (optional; its presence is what
#                                          switches ECFLOW_LOGVIEW on)
RUN pacman -Syu --noconfirm --disable-sandbox \
    gcc-fortran \
    boost \
    qt6-base \
    qt6-svg \
    qt6-5compat \
    qt6-charts \
 && pacman -Scc --noconfirm

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
