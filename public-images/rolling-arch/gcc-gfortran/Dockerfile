# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-arch-gcc-gfortran
FROM eccr.ecmwf.int/public-ci-images/rolling-arch-base:latest

# Unversioned, unlike ubuntu24.04-gcc13-gfortran13: on a rolling distro there is
# exactly one gcc and pinning a number in the name would start lying the first
# time upstream moves. `gcc --version` inside the image is the answer.
#
# The base carries no compiler; gcc is named here with the Fortran front end.
RUN pacman -Syu --noconfirm --disable-sandbox \
    gcc \
    gcc-fortran \
 && pacman -Scc --noconfirm

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
