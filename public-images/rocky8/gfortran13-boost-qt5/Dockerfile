# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-gfortran13-boost-qt5
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
#
# FROMs the shared base and installs its whole toolchain, rather than layering on
# rocky8-gfortran13: every variant is exactly one level above the base, because
# images.yml builds the base and then every dependent in a single parallel
# matrix, so a variant FROMing another variant would race on :latest.
FROM eccr.ecmwf.int/public-ci-images/rocky8-base:latest

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
# epel-release is pulled in for exactly one package: qt5-qtcharts-devel is not in
# AppStream or CRB, only in EPEL. Dropping charts instead would silently turn
# ECFLOW_LOGVIEW off here while it is on in the ubuntu image, which is the kind
# of per-platform difference that shows up as a missing feature rather than a
# build failure. Nothing else in this image comes from EPEL.
RUN dnf -y install epel-release \
 && dnf -y install \
    gcc-toolset-13 \
    boost-devel \
    qt5-qtbase-devel \
    qt5-qtsvg-devel \
    qt5-qtcharts-devel \
 && dnf clean all && rm -rf /var/cache/dnf

# See public-images/rocky8/gfortran13/Dockerfile for why PATH is set directly
# rather than through the toolset's `enable` script.
ENV PATH=/opt/rh/gcc-toolset-13/root/usr/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/rh/gcc-toolset-13/root/usr/lib64:/opt/rh/gcc-toolset-13/root/usr/lib
