# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-gfortran-boost-qt6
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# FROMs the shared base and installs its whole toolchain, rather than layering on
# rolling-gfortran: every variant is exactly one level above the base, because
# images.yml builds the base and then every dependent in a single parallel
# matrix, so a variant FROMing another variant would race on :latest.
FROM eccr.ecmwf.int/public-ci-images/rolling-base:latest

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
