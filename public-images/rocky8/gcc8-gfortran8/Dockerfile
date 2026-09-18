# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/rocky8-gcc8-gfortran8
FROM eccr.ecmwf.int/public-ci-images/rocky8-base:latest

# Rocky 8's OWN gcc, 8.5, not an SCL toolset under /opt/rh. That is the compiler
# this platform exists to test: it is the distro default, and it is what the
# Atos HPC GNU builds target, so a rocky 8 leg on anything newer tests a
# combination nobody ships. The base carries no compiler, so the whole GNU
# toolchain is installed here, where the image name accounts for it.
RUN dnf -y install gcc gcc-c++ gcc-gfortran \
 && dnf clean all && rm -rf /var/cache/dnf

# ubuntu and debian name compilers <tool>-<version>; the rocky RPMs do not, so
# `g++-8` -- what every manifest asks CMake for -- resolves nowhere. Match the
# convention, and keep the version in the artifact name, where a compiler bump
# invalidates the cache on its own.
RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-8; \
    done

ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
