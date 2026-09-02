# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/debian11-gfortran10
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/debian11-base:latest

ENV DEBIAN_FRONTEND=noninteractive

# Toolchain layered on the shared base, which already provides sudo, git, curl,
# cmake, build-essential, gh, python3.11, etc.
#
# 10 rather than the 12/13 the ubuntu variants carry: bullseye's newest gcc is
# 10, and that is the point of having this platform in the matrix at all.
RUN apt-get update && apt-get install -y \
    gcc-10 \
    g++-10 \
    gfortran-10 \
 && rm -rf /var/lib/apt/lists/*
