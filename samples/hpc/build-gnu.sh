#!/bin/bash

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

# Sample HPC build recipe: copy to a package's .ci/hpc/build-<toolchain>.sh and
# adapt. Name it after the toolchain it loads (build-gnu.sh, build-intel.sh, ...)
# even when the repo has only one -- a file called build.sh that loads prgenv/gnu
# is a generic name doing specific work.
#
# The repo owns everything above the build itself:
#   * the #SBATCH resource directives (qos / time / nodes / ntasks / gres),
#   * the `module load` lines for the toolchain.
#
# ci-infrastructure wraps this file when it submits the job: it appends
# `#SBATCH --output/--error` (so the poller can tail for the sentinel), exports
# the resolved dependency environment, and appends the success/failure sentinel.
# So this script can rely on:
#   * $CMAKE_PREFIX_PATH  — the resolved dependency install prefixes,
#   * $CI_INSTALL_PREFIX  — where to install this package's build.
# and it must NOT print "Finished: SUCCESS" / "Finished: FAILURE" itself.

# Resources below are tuned for ECMWF's atos (hpc2020): it selects on QoS rather
# than partition, and ssdtmp sizes the node-local SSD behind $TMPDIR, which holds
# the unpacked source and should hold the build too. Use plain #SBATCH lines:
# ci-infrastructure submits through troika's site API, which does not read its
# "# troika key=value" directives — only its controller does, and we bypass that.
#SBATCH --qos=nf
#SBATCH --gres=ssdtmp:30G
#SBATCH --time=00:30:00
#SBATCH --nodes=1
#SBATCH --ntasks=8

module load prgenv/gnu
module load cmake

# Build out of tree on the node-local SSD: $CI_SOURCE_DIR is there already, but
# a build dir inside the source tree would be tarred up by a later fetch.
cmake -B "${TMPDIR:-/tmp}/build" -S . \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$CI_INSTALL_PREFIX"
cmake --build "${TMPDIR:-/tmp}/build" --parallel "${SLURM_NTASKS:-8}"
ctest --test-dir "${TMPDIR:-/tmp}/build" --output-on-failure
cmake --install "${TMPDIR:-/tmp}/build"
