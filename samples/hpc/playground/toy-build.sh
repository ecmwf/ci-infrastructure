#!/bin/bash
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
