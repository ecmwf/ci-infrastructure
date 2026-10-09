# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
FROM eccr.ecmwf.int/public-ci-images/ubuntu24.04-base:latest

# The boost-qt6 image's packages, so ecflow's Rust bindings can build ecFlow too.
RUN apt-get update && apt-get install -y \
    gcc-13 \
    g++-13 \
    gfortran-13 \
    libboost-all-dev \
    qt6-base-dev \
    qt6-svg-dev \
    qt6-5compat-dev \
    qt6-charts-dev \
 && rm -rf /var/lib/apt/lists/*

# One pinned toolchain, the default, as g++-13 is the only g++. The proxies go
# next to uv in /usr/local/bin.
ENV RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --profile minimal --default-toolchain 1.90 --component clippy,rustfmt \
 && ln -s /opt/rust/cargo/bin/* /usr/local/bin/ \
 && rustc --version && cargo clippy --version && cargo fmt --version

# Re-declared in every image: an inherited copy would announce the base.
ARG IMAGE_NAME=""
ARG IMAGE_TAG=""
ARG IMAGE_CREATED=""
ARG IMAGE_DOCKERFILE_URL=""
ENV CI_IMAGE_NAME=${IMAGE_NAME} \
    CI_IMAGE_TAG=${IMAGE_TAG} \
    CI_IMAGE_CREATED=${IMAGE_CREATED} \
    CI_IMAGE_DOCKERFILE_URL=${IMAGE_DOCKERFILE_URL}
