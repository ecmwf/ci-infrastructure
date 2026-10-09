#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# verify-environment.sh <platform>/<variant>: checks the contract in public-images/README.md
# from INSIDE the image (build-image.sh --test, smoke-test-runners.yml).
# Must be the first bash of its container: BASH_ENV leaves the marker checked last.
#
# verify-environment.sh --host: the part a self-hosted runner or a custom image
# needs too (uv, CMake, headers, tools), without the official images' own checks.
set -euo pipefail

if [ "${1:-}" = "--host" ]; then
  mode=host
  DECLARES="$(hostname)"
else
  mode=image
  DECLARES="${1:?usage: verify-environment.sh <platform>/<variant> | --host}"
fi
# The floor libaec sets, which is the highest of any submodule we build.
MIN_CMAKE=3.26

fail() { echo "::error::$DECLARES: $*"; exit 1; }

if [ "$mode" = image ]; then
  : "${CI_INFRASTRUCTURE_PYTHON:?the image should set CI_INFRASTRUCTURE_PYTHON}"
  # ci-infrastructure's interpreter is its own: uv-managed under /opt/ci-infrastructure,
  # and never on PATH, so no build or job step picks it up.
  case "$CI_INFRASTRUCTURE_PYTHON" in
    /opt/ci-infrastructure/*) ;;
    *) fail "CI_INFRASTRUCTURE_PYTHON=$CI_INFRASTRUCTURE_PYTHON is not under /opt/ci-infrastructure" ;;
  esac
  case ":$PATH:" in
    *":$(dirname "$CI_INFRASTRUCTURE_PYTHON"):"*) fail "$(dirname "$CI_INFRASTRUCTURE_PYTHON") is on PATH" ;;
  esac
  "$CI_INFRASTRUCTURE_PYTHON" -I -c 'import ci_infrastructure' || fail "$CI_INFRASTRUCTURE_PYTHON cannot import ci_infrastructure"
  echo "python: $CI_INFRASTRUCTURE_PYTHON ($("$CI_INFRASTRUCTURE_PYTHON" -I -c 'import sys; print(sys.version.split()[0])'))"
fi

command -v uv >/dev/null || fail "uv is not on PATH"
if [ "$mode" = image ]; then
  : "${UV_PYTHON_INSTALL_DIR:?the image should set UV_PYTHON_INSTALL_DIR}"
fi
echo "uv: $(uv --version), Pythons for builds in ${UV_PYTHON_INSTALL_DIR:-the uv default}"

if [ "$mode" = image ]; then
  # Each reaches the image through a --build-arg that build-image.sh passes only
  # to Dockerfiles declaring the ARG, so broken wiring leaves it EMPTY, not absent.
  : "${CI_INFRASTRUCTURE_BAKED_REF:?the image should record which commit it baked}"
  : "${CI_IMAGE_NAME:?the image should record its own name}"
  : "${CI_IMAGE_TAG:?the image should record its own tag}"
  : "${CI_IMAGE_CREATED:?the image should record when it was built}"
  : "${CI_IMAGE_DOCKERFILE_URL:?the image should record a link to its Dockerfile}"
  # Catches a variant that did not re-declare CI_IMAGE_*.
  [ "$CI_IMAGE_NAME" = "$DECLARES" ] || fail "image reports itself as '$CI_IMAGE_NAME'"
  echo "image: $CI_IMAGE_NAME:$CI_IMAGE_TAG built $CI_IMAGE_CREATED, ci-infrastructure $CI_INFRASTRUCTURE_BAKED_REF"
fi

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

# As CMake's FindMPI does: the wrapper's flags with the named compiler, not the
# wrapper's default `gcc`. Four ranks, as the MPI tests ask for, on few cores.
expect_openmpi() {
  local cc="$1" out
  [ -n "$cc" ] || fail "'$variant' names openmpi but no gcc to build with"
  printf '%s\n' '#include <mpi.h>' '#include <stdio.h>' \
    'int main(int c, char** v) { int r; MPI_Init(&c, &v); MPI_Comm_rank(MPI_COMM_WORLD, &r);' \
    '  printf("rank %d\n", r); MPI_Finalize(); return 0; }' >"$omp_tmp/mpi.c"
  # shellcheck disable=SC2046
  "$cc" $(mpicc -showme:compile) "$omp_tmp/mpi.c" -o "$omp_tmp/mpi.bin" $(mpicc -showme:link) \
    || fail "$cc cannot build an MPI program with mpicc's flags"
  out="$(mpirun -np 4 "$omp_tmp/mpi.bin" 2>&1)" || { printf '%s\n' "$out" >&2; fail "mpirun -np 4 failed"; }
  [ "$(grep -c '^rank ' <<<"$out")" -eq 4 ] || { printf '%s\n' "$out" >&2; fail "mpirun -np 4 did not start 4 ranks"; }
  echo "openmpi: $(mpirun --version | head -1)"
}

# cargo links with the leg's C compiler, as a recipe sets it: the image has no `cc`.
expect_rust() {
  local version="$1" cc="$2" got host
  for tool in rustc cargo rustup; do
    command -v "$tool" >/dev/null || fail "$tool is not on PATH"
  done
  got="$(rustc --version | awk '{print $2}')"
  case "$got" in "$version" | "$version".*) ;; *) fail "rustc reports $got, expected $version" ;; esac
  cargo clippy --version >/dev/null || fail "cargo clippy is missing"
  cargo fmt --version >/dev/null || fail "cargo fmt is missing"
  [ -n "$cc" ] || fail "'$variant' names rust but no gcc to link with"
  host="$(rustc -vV | sed -n 's/^host: //p')"
  printf 'fn main() { println!("rust ok"); }\n' >"$omp_tmp/main.rs"
  rustc -C linker="$(command -v "$cc")" --target "$host" "$omp_tmp/main.rs" -o "$omp_tmp/rust.bin" \
    || fail "rustc cannot link a program with $cc"
  [ "$("$omp_tmp/rust.bin")" = "rust ok" ] || fail "rustc built a program that does not run"
  echo "rust: $(rustc --version), $(cargo --version)"
}

variant=""
[ "$mode" = image ] && variant="${DECLARES#*/}"
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

# What every base provides (docs/reference/runners.rst, "Keep the base images
# uniform"); the variants inherit it.
for header in zlib.h ncurses.h openssl/ssl.h; do
  [ -f "/usr/include/$header" ] || fail "/usr/include/$header is missing; every base lists the library providing it"
done
# Debian and Ubuntu install it under the multiarch directory.
compgen -G "/usr/include/curl/curl.h" >/dev/null || compgen -G "/usr/include/*/curl/curl.h" >/dev/null \
  || fail "curl/curl.h is missing; every base lists libcurl's development package"
missing=""
for tool in git gh curl wget cmake ninja make bison flex jq unzip zstd sudo gpg cmp diff ps; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
[ -z "$missing" ] || fail "missing from PATH:$missing; every base lists the package providing each"
echo "uniform: zlib, ncurses, OpenSSL and libcurl headers, git, gh, curl, wget, cmake, ninja, make, bison, flex, jq, unzip, zstd, sudo, gpg, diffutils, procps"

if [ "$mode" = image ]; then
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
      gcc[0-9]*)      expect_pair "gcc-${token#gcc}" "g++-${token#gcc}" "${token#gcc}"
                      gnu_cc="gcc-${token#gcc}" ;;
      gcc)            expect_pair gcc g++ ""
                      gnu_cc=gcc ;;
      clang[0-9]*)    expect_pair "clang-${token#clang}" "clang++-${token#clang}" "${token#clang}" ;;
      gfortran[0-9]*) expect_compiler "gfortran-${token#gfortran}" "${token#gfortran}"
                      expect_openmp "gfortran-${token#gfortran}" f ;;
      gfortran)       expect_compiler gfortran ""
                      expect_openmp gfortran f ;;
      openmpi)        expect_openmpi "${gnu_cc:-}" ;;
      rust[0-9]*)     expect_rust "${token#rust}" "${gnu_cc:-}" ;;
    esac
  done
fi

if [ "$mode" = image ]; then
  : "${BASH_ENV:?the image should point BASH_ENV at its announcer}"
  [ -r "$BASH_ENV" ] || fail "BASH_ENV=$BASH_ENV is not readable"
  [ -e "${CI_IMAGE_ANNOUNCED_MARKER:-/tmp/.ci-image-announced}" ] || fail "$BASH_ENV did not announce on entry"
  echo "image contract holds for $DECLARES"
else
  echo "environment contract holds for $DECLARES"
fi
