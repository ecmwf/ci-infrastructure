# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-gfortran10-boost-qt5
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# FROMs the shared base and installs its whole toolchain, rather than layering on
# debian11-gfortran10: every variant is exactly one level above the base, because
# images.yml builds the base and then every dependent in a single parallel
# matrix, so a variant FROMing another variant would race on :latest.
FROM eccr.ecmwf.int/public-ci-images/debian11-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Qt5, not Qt6 -- bullseye predates Qt6 entirely, there is no qt6-base-dev to
# install. ecflow's cmake/Dependencies.cmake accepts either ("Qt5/6 not found"),
# so this is a real substitution rather than a gap, and the image name says which
# one you get. The package set mirrors what
# ubuntu24.04-gfortran13-boost-qt6 installs:
#   qtbase5-dev        Widgets, Gui, Network   (required components)
#   libqt5svg5-dev     Svg                     (required component)
#   libqt5charts5-dev  Charts                  (optional; its presence is what
#                                               switches ECFLOW_LOGVIEW on)
# Qt5 needs no Core5Compat -- that module exists only to carry Qt5 APIs into Qt6.
RUN apt-get update && apt-get install -y \
    gcc-10 \
    g++-10 \
    gfortran-10 \
    libboost-all-dev \
    qtbase5-dev \
    libqt5svg5-dev \
    libqt5charts5-dev \
 && rm -rf /var/lib/apt/lists/*
