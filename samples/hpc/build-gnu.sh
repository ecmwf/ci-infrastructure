#!/bin/bash

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Sample plain HPC recipe; see docs/howto/hpc.rst. The wrapper exports $CMAKE_PREFIX_PATH,
# $CI_INSTALL_PREFIX and $CI_INSTALL_ARCHIVE and appends the sentinel.
# Plain #SBATCH lines: troika's site API ignores "# troika" directives.
#SBATCH --qos=nf
#SBATCH --gres=ssdtmp:30G
#SBATCH --time=00:30:00
#SBATCH --nodes=1
#SBATCH --ntasks=8

module load prgenv/gnu
module load cmake

cmake -B "${TMPDIR:-/tmp}/build" -S . \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"
cmake --build "${TMPDIR:-/tmp}/build" --parallel "${SLURM_NTASKS:-8}"
ctest --test-dir "${TMPDIR:-/tmp}/build" --output-on-failure
cmake --install "${TMPDIR:-/tmp}/build"

mkdir -p "$(dirname "$CI_INSTALL_ARCHIVE")"
tar -cf - -C "$CI_INSTALL_PREFIX" . | zstd -T0 -q -o "$CI_INSTALL_ARCHIVE.part"
mv "$CI_INSTALL_ARCHIVE.part" "$CI_INSTALL_ARCHIVE"
