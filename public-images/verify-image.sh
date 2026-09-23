#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# verify-image.sh <platform>/<variant> -- the contract every CI image keeps, run
# INSIDE the image: by build-image.sh --test before an image is published, and
# by smoke-test-runners.yml against the published image on the ARC runners.
#
# Must be the first bash of its container or job: the announcer marker checked
# at the end is left by BASH_ENV on the way into this script.
#
# One token of the variant's name per toolchain, nothing inferred from another
# token, and each must build and run an OpenMP program (see docs/howto/images.rst):
#   gcc<N>       gcc-N and g++-N, major N
#   clang<N>     clang-N and clang++-N, major N
#   gfortran<N>  gfortran-N, major N
#   gcc/gfortran (unversioned, rolling platforms) the same, without a version check
set -euo pipefail

DECLARES="${1:?usage: verify-image.sh <platform>/<variant>}"
# The floor libaec sets, which is the highest of any submodule we build.
MIN_CMAKE=3.26

fail() { echo "::error::$DECLARES: $*"; exit 1; }

: "${CI_INFRASTRUCTURE_PYTHON:?the image should set CI_INFRASTRUCTURE_PYTHON}"
"$CI_INFRASTRUCTURE_PYTHON" -c 'import ci_infrastructure' || fail "$CI_INFRASTRUCTURE_PYTHON cannot import ci_infrastructure"
echo "python: $CI_INFRASTRUCTURE_PYTHON ($("$CI_INFRASTRUCTURE_PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

# Each reaches the image through a --build-arg that build-image.sh passes only
# to Dockerfiles declaring the ARG, so broken wiring leaves it EMPTY, not absent.
: "${CI_INFRASTRUCTURE_BAKED_REF:?the image should record which commit it baked}"
: "${CI_IMAGE_NAME:?the image should record its own name}"
: "${CI_IMAGE_TAG:?the image should record its own tag}"
: "${CI_IMAGE_CREATED:?the image should record when it was built}"
: "${CI_IMAGE_DOCKERFILE_URL:?the image should record a link to its Dockerfile}"
# ENV is inherited, so a variant that failed to re-declare CI_IMAGE_* would
# silently answer with its base's name.
[ "$CI_IMAGE_NAME" = "$DECLARES" ] || fail "image reports itself as '$CI_IMAGE_NAME'"
echo "image: $CI_IMAGE_NAME:$CI_IMAGE_TAG built $CI_IMAGE_CREATED, ci-infrastructure $CI_INFRASTRUCTURE_BAKED_REF"

have="$(cmake --version | head -1 | awk '{print $3}')"
[ "$(printf '%s\n%s\n' "$MIN_CMAKE" "$have" | sort -V | head -1)" = "$MIN_CMAKE" ] \
  || fail "cmake $have is below the $MIN_CMAKE floor"
echo "cmake: $have"

expect_compiler() {
  local binary="$1" major="$2" got
  command -v "$binary" >/dev/null || fail "$binary is not on PATH"
  if [ -n "$major" ]; then
    got="$("$binary" -dumpversion | cut -d. -f1)"
    [ "$got" = "$major" ] || fail "$binary reports major $got, expected $major"
  fi
  echo "compiler: $(command -v "$binary") -> $("$binary" --version | head -1)"
}

# gcc and clang both come as a C/C++ pair; Fortran stands alone.
expect_pair() {
  expect_compiler "$1" "$3"
  expect_compiler "$2" "$3"
  expect_openmp "$1" c
  expect_openmp "$2" cxx
}

forbid() {
  ! command -v "$1" >/dev/null 2>&1 || fail "$(command -v "$1") $2"
}

omp_tmp="$(mktemp -d)"
trap 'rm -rf "$omp_tmp"' EXIT

# Compile, link AND run: the runtime library is the half that goes missing, and a
# link that resolves against a libomp the loader cannot find still fails in a job.
expect_openmp() {
  local binary="$1" lang="$2" src out
  case "$lang" in
    c)   src="$omp_tmp/omp.c" ;;
    cxx) src="$omp_tmp/omp.cxx" ;;
    f)   src="$omp_tmp/omp.f90" ;;
    *)   fail "expect_openmp: unknown language '$lang'" ;;
  esac
  if [ "$lang" = f ]; then
    cat > "$src" <<'EOF'
program main
    use omp_lib
    implicit none
    integer :: threads
    threads = 0
    !$omp parallel
    !$omp atomic
    threads = threads + 1
    !$omp end parallel
    if (threads < 1) stop 1
end program main
EOF
  else
    cat > "$src" <<'EOF'
#include <omp.h>
int main(void) {
    int threads = 0;
    #pragma omp parallel
    {
        #pragma omp atomic
        ++threads;
    }
    return threads < 1;
}
EOF
  fi
  if ! out="$("$binary" -fopenmp "$src" -o "$omp_tmp/omp.bin" 2>&1)"; then
    printf '%s\n' "$out" >&2
    fail "$binary cannot build an OpenMP program (-fopenmp)"
  fi
  if ! out="$(OMP_NUM_THREADS=2 "$omp_tmp/omp.bin" 2>&1)"; then
    printf '%s\n' "$out" >&2
    fail "$binary built an OpenMP program that does not run"
  fi
  echo "openmp: $binary"
}

variant="${DECLARES#*/}"
declares_gcc=""
for token in ${variant//-/ }; do
  case "$token" in gcc|gcc[0-9]*) declares_gcc=1 ;; esac
done

if [ "$variant" = base ]; then
  for binary in cc c++ gcc g++ clang clang++ gfortran; do
    forbid "$binary" "is a compiler in a base image; those belong to a named variant"
  done
  echo "base: no compilers, as intended"
fi

# gfortran-N Depends on gcc-N, so a GNU toolchain can arrive as another package's
# dependency and become the cc a build silently picks up.
if [ -z "$declares_gcc" ]; then
  for binary in cc c++ gcc g++; do
    forbid "$binary" "exists but '$variant' names no gcc; name it or stop installing it"
  done
  for path in /usr/bin/gcc-* /usr/bin/g++-* /usr/local/bin/gcc-* /usr/local/bin/g++-*; do
    [ -e "$path" ] || continue
    fail "$path exists but '$variant' names no gcc; name it or stop installing it"
  done
fi

for token in ${variant//-/ }; do
  case "$token" in
    gcc[0-9]*)      expect_pair "gcc-${token#gcc}" "g++-${token#gcc}" "${token#gcc}" ;;
    gcc)            expect_pair gcc g++ "" ;;
    clang[0-9]*)    expect_pair "clang-${token#clang}" "clang++-${token#clang}" "${token#clang}" ;;
    gfortran[0-9]*) expect_compiler "gfortran-${token#gfortran}" "${token#gfortran}"
                    expect_openmp "gfortran-${token#gfortran}" f ;;
    gfortran)       expect_compiler gfortran ""
                    expect_openmp gfortran f ;;
  esac
done

: "${BASH_ENV:?the image should point BASH_ENV at its announcer}"
[ -r "$BASH_ENV" ] || fail "BASH_ENV=$BASH_ENV is not readable"
[ -e "${CI_IMAGE_ANNOUNCED_MARKER:-/tmp/.ci-image-announced}" ] || fail "$BASH_ENV did not announce on entry"
echo "image contract holds for $DECLARES"
