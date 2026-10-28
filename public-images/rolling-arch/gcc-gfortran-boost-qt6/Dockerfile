# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/rolling-arch-base:latest

RUN pacman -Syu --noconfirm --disable-sandbox \
    gcc \
    gcc-fortran \
    boost \
    boost-libs \
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
