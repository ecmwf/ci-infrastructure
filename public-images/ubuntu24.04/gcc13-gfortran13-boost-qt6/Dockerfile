# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/ubuntu24.04-gcc13-gfortran13-boost-qt6
# Boost and Qt are large, so only images that name them carry them.
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

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
    gcc-13 \
    g++-13 \
    gfortran-13 \
    libboost-all-dev \
    qt6-base-dev \
    qt6-svg-dev \
    qt6-5compat-dev \
    qt6-charts-dev \
 && rm -rf /var/lib/apt/lists/*

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
