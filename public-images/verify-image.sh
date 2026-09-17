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
#   clang<N>     clang++-N, major N
#   gfortran<N>  gfortran-N, major N, and g++-N unless a clang<N> names the C++ compiler
#   gfortran     (unversioned, rolling platforms) gfortran and g++ on PATH
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

variant="${DECLARES#*/}"
clang=""
for token in ${variant//-/ }; do
  case "$token" in
    clang[0-9]*) clang="${token#clang}"; expect_compiler "clang++-$clang" "$clang" ;;
  esac
done
for token in ${variant//-/ }; do
  case "$token" in
    gfortran[0-9]*)
      expect_compiler "gfortran-${token#gfortran}" "${token#gfortran}"
      [ -n "$clang" ] || expect_compiler "g++-${token#gfortran}" "${token#gfortran}"
      ;;
    gfortran)
      expect_compiler gfortran ""
      [ -n "$clang" ] || expect_compiler g++ ""
      ;;
  esac
done

: "${BASH_ENV:?the image should point BASH_ENV at its announcer}"
[ -r "$BASH_ENV" ] || fail "BASH_ENV=$BASH_ENV is not readable"
[ -e "${CI_IMAGE_ANNOUNCED_MARKER:-/tmp/.ci-image-announced}" ] || fail "$BASH_ENV did not announce on entry"
echo "image contract holds for $DECLARES"
