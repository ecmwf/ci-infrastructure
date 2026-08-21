# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-playground-ci/ubuntu24.04-gfortran13-boost-qt6
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# FROMs the shared base and installs its whole toolchain, rather than layering on
# ubuntu24.04-gfortran13: every variant here is exactly one level above the base
# (clang18-gfortran13 likewise installs gfortran-13 itself instead of building on
# the gfortran13 variant). images.yml depends on that — it builds the base,
# then every dependent in a single parallel matrix, so a variant FROMing another
# variant could pull a stale :latest of its own base.
#
# Boost and Qt are here rather than in the base because both are large installs
# that only packages actually linking them need; OpenSSL, being small and wanted
# by several, lives in the base. So this image's name says exactly what it adds
# over base.
FROM eccr.ecmwf.int/public-playground-ci/ubuntu24.04-base:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Toolchain layered on the shared base, which already provides sudo, git, curl,
# cmake, build-essential, gh, python, libssl-dev, etc.
#
# The Qt packages are exactly what ecflow's cmake/Dependencies.cmake looks for
# under ENABLE_UI (ON by default), which is a hard ecbuild_critical if Qt is
# absent — the HPC lane gets the same coverage from `module load qt/6.6.1`:
#   qt6-base-dev     Widgets, Gui, Network   (required components)
#   qt6-svg-dev      Svg                     (required component)
#   qt6-5compat-dev  Core5Compat             (required component)
#   qt6-charts-dev   Charts                  (optional; its presence is what
#                                             switches ECFLOW_LOGVIEW on, so
#                                             installing it keeps the server log
#                                             viewer in the build)
RUN apt-get update && apt-get install -y \
    gfortran-13 \
    libboost-all-dev \
    qt6-base-dev \
    qt6-svg-dev \
    qt6-5compat-dev \
    qt6-charts-dev \
 && rm -rf /var/lib/apt/lists/*
