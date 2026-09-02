# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-gfortran
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/rolling-base:latest

# Unversioned, unlike ubuntu24.04-gfortran13: on a rolling distro there is
# exactly one gcc and pinning a number in the name would start lying the first
# time upstream moves. `gcc --version` inside the image is the answer.
#
# gcc itself comes with base-devel in the base; this adds the Fortran front end.
RUN pacman -Syu --noconfirm --disable-sandbox \
    gcc-fortran \
 && pacman -Scc --noconfirm
