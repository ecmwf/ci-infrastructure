# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/fedora43-base:latest

# gcc15-gfortran15, plus what release builds link and package with (README "Release images").
# All in Fedora itself, except libaec (below).
RUN dnf -y install gcc gcc-c++ gcc-gfortran \
    rpm-build \
    bzip2-devel \
    lz4-devel \
    blas-devel \
    lapack-devel \
    eigen3-devel \
 && dnf clean all && rm -rf /var/cache/dnf

RUN for t in gcc g++ gfortran cpp; do \
      ln -s /usr/bin/$t /usr/local/bin/$t-15; \
    done

# libaec from source: Fedora 43's 1.1.4 has a CMake config that needs a libaec.a it does not ship (README "Release images").
# Static only, so a package embeds it and needs no libaec on the target. Upstream already sets
# POSITION_INDEPENDENT_CODE; the flag here states that the static library must stay PIC.
# Its CMake config loads the shared targets unless libaec_USE_STATIC_LIBS is ON, and only the
# static ones are installed, so the installed config sets it.
ARG LIBAEC_VERSION=1.1.7
ARG LIBAEC_SHA256=cc7b93be9002e25a88b45d6b8d5f6120756cb5e1000613f91d803ce0beba24d9
RUN cd /tmp \
 && curl -fsSLO "https://github.com/Deutsches-Klimarechenzentrum/libaec/releases/download/v${LIBAEC_VERSION}/libaec-${LIBAEC_VERSION}.tar.gz" \
 && echo "${LIBAEC_SHA256}  libaec-${LIBAEC_VERSION}.tar.gz" | sha256sum -c - \
 && cmake -E tar xzf "libaec-${LIBAEC_VERSION}.tar.gz" \
 && cmake -S "libaec-${LIBAEC_VERSION}" -B libaec-build \
      -DCMAKE_C_COMPILER=gcc-15 -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_INSTALL_LIBDIR=lib \
      -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON \
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_TESTING=OFF \
 && cmake --build libaec-build -j"$(nproc)" \
 && cmake --install libaec-build \
 && cfg=/usr/local/lib/cmake/libaec/libaec-config.cmake \
 && { printf '%s\n' '# ci-infrastructure: only the static libraries are installed.' \
        'set(libaec_USE_STATIC_LIBS ON)' ''; cat "$cfg"; } >"$cfg.new" \
 && mv "$cfg.new" "$cfg" \
 && rm -rf "libaec-${LIBAEC_VERSION}" libaec-build "libaec-${LIBAEC_VERSION}.tar.gz"

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
