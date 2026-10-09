# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""cargo_patches: git crates follow the branch resolve-deps took their repo from."""

from __future__ import annotations

import json
from typing import Any, Final

from click.testing import CliRunner

from ci_infrastructure.cargo_patches import main, patches

_ECKIT_SSH: Final = "git+ssh://git@github.com/ecmwf/eckit.git?branch=develop#" + "a" * 40
_METADATA: Final[dict[str, Any]] = {
    "packages": [
        {"name": "eckit-sys", "source": _ECKIT_SSH},
        {"name": "eckit", "source": _ECKIT_SSH},
        {
            "name": "eccodes-sys",
            "source": "git+ssh://git@github.com/ecmwf/rust-wrappers-playground.git?rev=d6b582fd#d6b",
        },
        {"name": "metkit-sys", "source": "git+https://github.com/ecmwf/metkit?branch=develop#" + "b" * 40},
        {"name": "thiserror", "source": "registry+https://github.com/rust-lang/crates.io-index"},
        {"name": "fdb", "source": None},
    ]
}


def test_a_sync_branch_patches_every_crate_of_its_repo() -> None:
    url = 'patch."ssh://git@github.com/ecmwf/eckit.git"'
    assert list(patches(_METADATA, {"ecmwf/eckit": "sync-branch/x"})) == [
        f'{url}.eckit-sys.git="https://github.com/ecmwf/eckit.git"',
        f'{url}.eckit-sys.branch="sync-branch/x"',
        f'{url}.eckit.git="https://github.com/ecmwf/eckit.git"',
        f'{url}.eckit.branch="sync-branch/x"',
    ]


def test_an_https_source_is_patched_over_ssh_and_a_pin_by_rev() -> None:
    sha = "c" * 40
    assert list(patches(_METADATA, {"ecmwf/metkit": sha})) == [
        'patch."https://github.com/ecmwf/metkit".metkit-sys.git="ssh://git@github.com/ecmwf/metkit.git"',
        f'patch."https://github.com/ecmwf/metkit".metkit-sys.rev="{sha}"',
    ]


def test_a_normal_ref_or_an_unrelated_repo_patches_nothing() -> None:
    assert list(patches(_METADATA, {"ecmwf/eckit": "develop", "ecmwf/eccodes": "feature/y"})) == []


def test_the_cli_reads_metadata_and_refs() -> None:
    result = CliRunner().invoke(main, ["--ref", "ecmwf/eckit=feature/z"], input=json.dumps(_METADATA))
    assert result.exit_code == 0, result.output
    assert result.output.count("\n") == 4 and '.branch="feature/z"' in result.output
