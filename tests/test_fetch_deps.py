# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""fetch_deps: a needs-python wheel is installed into the consumer's interpreter, never sys.executable."""

from __future__ import annotations

import json
from typing import Final

from click.testing import CliRunner

from ci_infrastructure import fetch_deps

_DEP: Final = {
    "name": "cxxmath-python",
    "repo": "org/downstream-ci-repo-cxx-py",
    "ref": "main",
    "sha": "deadbeef",
    "artifact-name": "cxxmath-python-hpc-atos-gnu-Release-deadbeef",
    "cached": True,
    "source": "store",
    "install-path": "/tmp/install/cxxmath-python",
    "needs-python": True,
}


def test_needs_python_without_consumer_python_fails_loud() -> None:
    result = CliRunner().invoke(fetch_deps.main, ["--deps-json", json.dumps([_DEP])])
    assert result.exit_code != 0
    assert "cxxmath-python" in result.output
    assert "--consumer-python" in result.output
    assert "install-python-deps" in result.output
