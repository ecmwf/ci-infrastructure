# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""cargo_patches: the crates the manifest lists follow the commit their repo's C++ was built from."""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from click.testing import CliRunner

from ci_infrastructure.cargo_patches import main, patches, unused

_SHA: Final = "a" * 40
_CRATES_IO: Final = "registry+https://github.com/rust-lang/crates.io-index"
_SHAS: Final = {"ecmwf/eckit": _SHA, "ecmwf/ecbuild": "b" * 40}


def _declared(*deps: tuple[str, str | None]) -> dict[str, Any]:
    member = {
        "id": "path+file:///w/crates/foo-sys#1.0.0",
        "name": "foo-sys",
        "dependencies": [{"name": n, "source": s, "req": "^2.5"} for n, s in deps],
    }
    return {"packages": [member], "workspace_members": [member["id"]]}


def test_a_crates_io_dependency_is_patched_to_the_built_commit() -> None:
    metadata = _declared(("eckit-sys", _CRATES_IO), ("thiserror", _CRATES_IO), ("bar", None))
    assert list(patches(metadata, {"eckit-sys": "ecmwf/eckit"}, _SHAS)) == [
        'patch.crates-io.eckit-sys.git="https://github.com/ecmwf/eckit.git"',
        f'patch.crates-io.eckit-sys.rev="{_SHA}"',
    ]


def test_a_git_dependency_is_patched_from_another_url() -> None:
    metadata = _declared(("eckit-sys", "git+ssh://git@github.com/ecmwf/eckit.git?branch=develop"))
    url = 'patch."ssh://git@github.com/ecmwf/eckit.git"'
    assert list(patches(metadata, {"eckit-sys": "ecmwf/eckit"}, _SHAS)) == [
        f'{url}.eckit-sys.git="https://github.com/ecmwf/eckit.git"',
        f'{url}.eckit-sys.rev="{_SHA}"',
    ]
    https = _declared(("eckit-sys", "git+https://github.com/ecmwf/eckit"))
    assert next(patches(https, {"eckit-sys": "ecmwf/eckit"}, _SHAS)).endswith(
        '.git="ssh://git@github.com/ecmwf/eckit.git"'
    )


def test_a_listed_crate_cargo_toml_does_not_use_is_refused() -> None:
    with pytest.raises(Exception, match="no Cargo.toml depends on: eckit-sys"):
        list(patches(_declared(("thiserror", _CRATES_IO)), {"eckit-sys": "ecmwf/eckit"}, _SHAS))


def test_an_unlisted_git_crate_from_a_dep_repo_is_refused() -> None:
    metadata = _declared(
        ("eckit-sys", "git+ssh://git@github.com/ecmwf/eckit.git?branch=develop"),
        ("bindman-utils", "git+ssh://git@github.com/ecmwf/bindman.git?rev=34ef5ce"),
    )
    with pytest.raises(Exception, match="takes eckit-sys over git"):
        list(patches(metadata, {}, _SHAS))
    # Not a dep of the leg: Cargo.toml's ref stays.
    assert list(patches(_declared(("bindman-utils", "git+https://github.com/ecmwf/bindman")), {}, _SHAS)) == []


def test_a_crate_from_a_path_is_refused() -> None:
    with pytest.raises(Exception, match="eckit-sys: Cargo.toml takes it from a path"):
        list(patches(_declared(("eckit-sys", None)), {"eckit-sys": "ecmwf/eckit"}, _SHAS))


def test_an_unused_patch_names_the_requirement() -> None:
    def resolved(source: str) -> dict[str, Any]:
        metadata = _declared(("eckit-sys", _CRATES_IO))
        metadata["packages"].append({"id": "x", "name": "eckit-sys", "source": source})
        return metadata

    patched = f"git+https://github.com/ecmwf/eckit.git?rev={_SHA}#{_SHA}"
    assert unused(resolved(patched), {"eckit-sys": "ecmwf/eckit"}, _SHAS) == []
    assert unused(resolved(_CRATES_IO), {"eckit-sys": "ecmwf/eckit"}, _SHAS) == [
        f"eckit-sys: Cargo.toml requires ^2.5, which ecmwf/eckit@{_SHA} does not provide"
    ]


def test_the_cli_patches_and_checks() -> None:
    args = ["--crate", "eckit-sys=ecmwf/eckit", "--dep", f"ecmwf/eckit@{_SHA}"]
    metadata = _declared(("eckit-sys", _CRATES_IO))
    result = CliRunner().invoke(main, args, input=json.dumps(metadata))
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[1] == f'patch.crates-io.eckit-sys.rev="{_SHA}"'
    metadata["packages"].append({"id": "x", "name": "eckit-sys", "source": _CRATES_IO})
    result = CliRunner().invoke(main, [*args, "--check"], input=json.dumps(metadata))
    assert result.exit_code != 0 and "cargo did not use the patch for eckit-sys" in result.output


def test_the_cli_names_a_failed_cargo_metadata() -> None:
    result = CliRunner().invoke(main, ["--check"], input="")
    assert result.exit_code != 0 and "cargo failed above" in result.output
