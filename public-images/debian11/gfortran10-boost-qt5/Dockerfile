# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-gfortran10-boost-qt5
#
# TEMPORARY, superseded by debian11/gcc10-gfortran10-boost-qt5 — see "Legacy
# names" in IMAGES.md before touching this file.
FROM eccr.ecmwf.int/public-ci-images/debian11-base:latest

# Qt5, not Qt6 -- bullseye predates Qt6 entirely, there is no qt6-base-dev to
# install. ecflow's cmake/Dependencies.cmake accepts either ("Qt5/6 not found"),
# so this is a real substitution rather than a gap, and the image name says which
# one you get. The package set mirrors what
# ubuntu24.04-gcc13-gfortran13-boost-qt6 installs:
#   qtbase5-dev        Widgets, Gui, Network   (required components)
#   libqt5svg5-dev     Svg                     (required component)
#   libqt5charts5-dev  Charts                  (optional; its presence is what
#                                               switches ECFLOW_LOGVIEW on)
# Qt5 needs no Core5Compat -- that module exists only to carry Qt5 APIs into Qt6.
#
# build-essential is what this image used to inherit from the base.
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc-10 \
    g++-10 \
    gfortran-10 \
    libboost-all-dev \
    qtbase5-dev \
    libqt5svg5-dev \
    libqt5charts5-dev \
 && rm -rf /var/lib/apt/lists/*

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
