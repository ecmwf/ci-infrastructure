# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-gfortran8-boost-qt5
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# FROMs the shared base and installs its whole toolchain, rather than layering on
# rocky8-gfortran8: every variant is exactly one level above the base, because
# images.yml builds the base and then every dependent in a single parallel
# matrix, so a variant FROMing another variant would race on :latest.
FROM eccr.ecmwf.int/public-ci-images/rocky8-base:latest

# Rocky 8's OWN gcc, 8.5 -- see public-images/rocky8/gfortran8/Dockerfile for why
# the distro default rather than an SCL toolset. The base already installs gcc
# and gcc-c++; only Fortran is missing from it.
#
# Qt5, not Qt6 -- rocky 8 has no Qt6 at all, in AppStream or CRB. ecflow's
# cmake/Dependencies.cmake accepts either ("Qt5/6 not found"), so this is a real
# substitution rather than a gap, and the image name says which one you get.
# The package set is the Qt5 counterpart of what ubuntu24.04-gfortran13-boost-qt6
# installs:
#   qt5-qtbase-devel    Widgets, Gui, Network   (required components)
#   qt5-qtsvg-devel     Svg                     (required component)
#   qt5-qtcharts-devel  Charts                  (optional; its presence is what
#                                                switches ECFLOW_LOGVIEW on)
# Qt5 needs no Core5Compat -- that module exists only to carry Qt5 APIs into Qt6.
#
# boost-devel here is 1.66, which is exactly ECFLOW_BOOST_VERSION
# (ecflow cmake/Dependencies.cmake:180, whose comment names rocky 8 as the reason
# that floor is where it is). So this image is the thing that keeps that floor
# honest rather than merely satisfying it.
#
# boost-static (CRB, enabled in the base) is there for a single component:
# ecflow asks find_package for test_exec_monitor, and boost builds that one
# static-only, so rocky 8 ships it as libboost_test_exec_monitor.a in
# boost-static while boost-devel carries only .so files -- unlike libboost-all-dev
# on ubuntu/debian, which bundles the .a alongside. Without it, configure fails
# with 'Could NOT find Boost (missing: test_exec_monitor)'. Everything else stays
# shared: ENABLE_STATIC_BOOST_LIBS=OFF leaves CMake preferring .so where both exist.
# epel-release is pulled in for exactly one package: qt5-qtcharts-devel is not in
# AppStream or CRB, only in EPEL. Dropping charts instead would silently turn
# ECFLOW_LOGVIEW off here while it is on in the ubuntu image, which is the kind
# of per-platform difference that shows up as a missing feature rather than a
# build failure. Nothing else in this image comes from EPEL.
RUN dnf -y install epel-release \
 && dnf -y install \
    gcc-gfortran \
    boost-devel \
    boost-static \
    qt5-qtbase-devel \
    qt5-qtsvg-devel \
    qt5-qtcharts-devel \
 && dnf clean all && rm -rf /var/cache/dnf

# ubuntu and debian name compilers <tool>-<version>; the rocky RPMs do not, so
# `g++-8` -- what every manifest asks CMake for -- resolves nowhere. Match the
# convention, and keep the version in the artifact name, where a compiler bump
# invalidates the cache on its own.
RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-8; \
    done

# Self-description. The same facts build-image.sh writes as OCI labels, baked in
# as environment as well: a label can only be read from OUTSIDE the image
# (registry or daemon), and a job running INSIDE it has neither.
# announce-image.sh and actions/announce-image print these. See IMAGES.md.
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
