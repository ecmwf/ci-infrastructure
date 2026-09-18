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
# The compiler expectations come from the variant's name, so an image cannot
# promise a toolchain in its name that it does not ship:
#   clang<N>     clang-N and clang++-N, major N
#   gfortran<N>  gfortran-N, major N, and gcc-N/g++-N unless a clang<N> names C and C++
#   gfortran     (unversioned, rolling platforms) gcc, g++ and gfortran on PATH
#
# Every compiler named must also build and run an OpenMP program. GCC ships omp.h
# and libgomp with the compiler, clang splits them into libomp-<N>-dev, so a
# toolchain can satisfy its name and still have no OpenMP at all.
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
clang=""
for token in ${variant//-/ }; do
  case "$token" in
    clang[0-9]*)
      clang="${token#clang}"
      expect_compiler "clang-$clang" "$clang"
      expect_compiler "clang++-$clang" "$clang"
      expect_openmp "clang-$clang" c
      expect_openmp "clang++-$clang" cxx
      ;;
  esac
done
for token in ${variant//-/ }; do
  case "$token" in
    gfortran[0-9]*)
      gnu="${token#gfortran}"
      expect_compiler "gfortran-$gnu" "$gnu"
      expect_openmp "gfortran-$gnu" f
      if [ -z "$clang" ]; then
        expect_compiler "gcc-$gnu" "$gnu"
        expect_compiler "g++-$gnu" "$gnu"
        expect_openmp "gcc-$gnu" c
        expect_openmp "g++-$gnu" cxx
      fi
      ;;
    gfortran)
      expect_compiler gfortran ""
      expect_openmp gfortran f
      if [ -z "$clang" ]; then
        expect_compiler gcc ""
        expect_compiler g++ ""
        expect_openmp gcc c
        expect_openmp g++ cxx
      fi
      ;;
  esac
done

: "${BASH_ENV:?the image should point BASH_ENV at its announcer}"
[ -r "$BASH_ENV" ] || fail "BASH_ENV=$BASH_ENV is not readable"
[ -e "${CI_IMAGE_ANNOUNCED_MARKER:-/tmp/.ci-image-announced}" ] || fail "$BASH_ENV did not announce on entry"
echo "image contract holds for $DECLARES"
