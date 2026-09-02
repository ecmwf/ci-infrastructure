# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rolling-arch-gfortran-boost-qt6
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# FROMs the shared base and installs its whole toolchain, rather than layering on
# rolling-gfortran: every variant is exactly one level above the base, because
# images.yml builds the base and then every dependent in a single parallel
# matrix, so a variant FROMing another variant would race on :latest.
FROM eccr.ecmwf.int/public-ci-images/rolling-arch-base:latest

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

# Self-description. The same facts build-image.sh writes as OCI labels, baked in
# as environment as well: a label can only be read from OUTSIDE the image
# (registry or daemon), and a job running INSIDE it has neither.
# actions/announce-image prints these. See IMAGES.md.
#
# Declared LAST so a new value never busts the cache of the layers above, and
# repeated in EVERY image rather than inherited: ENV is inherited by descendants,
# so a variant that did not re-declare these would announce itself as its base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
