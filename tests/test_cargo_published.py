# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""cargo_published: the crates a release leaves out, as the registry has them already."""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from click.testing import CliRunner

from ci_infrastructure import cargo_published
from ci_infrastructure.cargo_published import index_path, main, published

_METADATA: Final[dict[str, Any]] = {
    "packages": [
        {"name": "foo-sys", "version": "2.0.0"},
        {"name": "foo", "version": "2.0.0"},
        {"name": "foo-compat", "version": "0.1.0"},
    ]
}
_REGISTRY: Final[dict[str, set[str]]] = {"foo-compat": {"0.1.0"}, "foo": {"1.0.0"}}


def _versions_of(name: str) -> set[str]:
    return _REGISTRY.get(name, set())


def test_a_named_crate_the_registry_has_at_its_version() -> None:
    assert published(_METADATA, ["foo-compat", "foo"], _versions_of) == ["foo-compat"]
    assert published(_METADATA, [], _versions_of) == []


def test_a_name_outside_the_workspace_is_refused() -> None:
    with pytest.raises(Exception, match="not a crate of the workspace: bar"):
        published(_METADATA, ["bar"], _versions_of)


def test_the_sparse_index_path() -> None:
    assert index_path("a") == "1/a"
    assert index_path("ab") == "2/ab"
    assert index_path("abc") == "3/a/abc"
    assert index_path("foo-sys") == "fo/o-/foo-sys"
    assert index_path("Serde") == "se/rd/serde"


def test_the_cli_reads_metadata_and_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cargo_published, "versions_in", lambda index, name: _versions_of(name))
    result = CliRunner().invoke(main, ["foo-compat", "foo-sys"], input=json.dumps(_METADATA))
    assert result.exit_code == 0, result.output
    assert result.output == "foo-compat\n"
