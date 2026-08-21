# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-playground-ci/ubuntu24.04-clang18-gfortran13
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-playground-ci/ubuntu24.04-base:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Toolchain layered on the shared base, which already provides sudo, git, curl,
# cmake, build-essential, gh, python, etc.
RUN apt-get update && apt-get install -y \
    gfortran-13 \
    clang++-18 \
    clang-tidy-18 \
 && rm -rf /var/lib/apt/lists/*
