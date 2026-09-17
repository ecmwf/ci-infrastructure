# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
# Published as eccr.ecmwf.int/public-ci-images/fedora44-gfortran16
# Built/pushed by .github/workflows/images.yml (see IMAGES.md).
FROM eccr.ecmwf.int/public-ci-images/fedora44-base:latest

RUN dnf -y install gcc-gfortran \
 && dnf clean all && rm -rf /var/cache/dnf

# Fedora packages expose unversioned compiler names. Add the versioned names used
# by downstream manifests while retaining the distro defaults.
RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-16; \
    done

# Self-description must be re-declared by every descendant image.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
