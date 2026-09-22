#!/usr/bin/env bash
#
# build-image.sh — build, test and push the CI images under public-images/.
#
# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0
#
# The entry point; the logic and its documentation live in scripts/build_image.py
# (./build-image.sh --help). PYTHON overrides the interpreter, which must be 3.11.4+.
set -euo pipefail

python="${PYTHON:-python3}"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11, 4))' 2>/dev/null || {
  echo "::error::build-image.sh needs Python >= 3.11.4 as '$python'; set PYTHON to one" >&2
  exit 1
}
exec "$python" "$(dirname "${BASH_SOURCE[0]}")/scripts/build_image.py" "$@"
