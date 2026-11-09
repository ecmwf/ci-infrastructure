#!/bin/sh
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Builds libaec from source into /usr/local for release images whose distribution libaec is
# below eckit's 1.1.4 or broken. Static only, so a package embeds it.
# Usage: build-libaec.sh <cc>
set -eu

version=1.1.7
sha256=cc7b93be9002e25a88b45d6b8d5f6120756cb5e1000613f91d803ce0beba24d9
src="libaec-$version"

tmp="$(mktemp -d)"
cd "$tmp"
curl -fsSLO "https://github.com/Deutsches-Klimarechenzentrum/libaec/releases/download/v$version/$src.tar.gz"
echo "$sha256  $src.tar.gz" | sha256sum -c -
cmake -E tar xzf "$src.tar.gz"
cmake -S "$src" -B build \
  -DCMAKE_C_COMPILER="$1" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_INSTALL_LIBDIR=lib \
  -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_TESTING=OFF
cmake --build build -j"$(nproc)"
cmake --install build

# The config loads the shared targets unless libaec_USE_STATIC_LIBS is set; only static exist.
cfg=/usr/local/lib/cmake/libaec/libaec-config.cmake
{ echo 'set(libaec_USE_STATIC_LIBS ON)'; cat "$cfg"; } >"$cfg.new"
mv "$cfg.new" "$cfg"

rm -rf "$tmp"
