#!/bin/bash

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# (C) Copyright 2026 - ECMWF and individual contributors.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation nor
# does it submit to any jurisdiction.

# The smallest possible HPC "build": no cmake, no modules, no compiler.
#
# That is the point. toy_roundtrip.py drives the real orchestration around this
# script (submit -> ship source -> marker -> unpack -> build -> fetch install),
# so if the run fails, the orchestration is what broke — a toolchain cannot be
# blamed. Adjust the resources below first if the job never leaves PENDING.

#SBATCH --qos=nf
#SBATCH --time=00:05:00
#SBATCH --ntasks=1
#SBATCH --gres=ssdtmp:10G

echo "toy: running on $(hostname) as $(id -un), SLURM_JOB_ID=${SLURM_JOB_ID:-<none>}"
echo "toy: TMPDIR=${TMPDIR:-<unset>}"
echo "toy: CI_SOURCE_DIR=$CI_SOURCE_DIR"
echo "toy: CI_INSTALL_PREFIX=$CI_INSTALL_PREFIX"
echo "toy: source tree the runner shipped:"
ls -la "$CI_SOURCE_DIR"

# Proves the shipped bytes arrived intact, not just that a directory exists.
echo "toy: contents of hello.txt:"
cat "$CI_SOURCE_DIR/hello.txt"

mkdir -p "$CI_INSTALL_PREFIX/bin"
cat > "$CI_INSTALL_PREFIX/bin/toy-result.txt" <<EOF
built on $(hostname) at $(date -Is)
slurm job: ${SLURM_JOB_ID:-<none>}
source came from: $CI_SOURCE_DIR
EOF
echo "toy: wrote $CI_INSTALL_PREFIX/bin/toy-result.txt"
