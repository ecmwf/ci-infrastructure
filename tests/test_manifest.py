# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from ci_infrastructure._github_api import ManifestSchemaError
from ci_infrastructure.manifest import (
    DepTable,
    DownstreamGateTable,
    GeneratedTable,
    ManifestFile,
    MatrixKindTable,
    PackageTable,
    TriggerDownstreamTable,
    validate,
)

_PACKAGE: dict[str, Any] = {"name": "a", "prefix": "a", "repo": "o/a", "compiler-inputs": []}
_DEP: dict[str, Any] = {"repo": "o/b", "package": "b", "ref": "main", "compiler-inputs": []}


@pytest.mark.parametrize(
    "table",
    [PackageTable, DepTable, TriggerDownstreamTable, MatrixKindTable, GeneratedTable, DownstreamGateTable],
)
def test_every_key_is_described(table: type[BaseModel]) -> None:
    """The manifest reference is generated from these descriptions."""
    assert table.__doc__
    assert [name for name, f in table.model_fields.items() if not f.description] == []


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"package": _PACKAGE, "pakage": {}}, r"pakage"),
        ({"package": {**_PACKAGE, "prefx": "a"}}, r"\[package\]\.prefx"),
        ({"package": _PACKAGE, "deps": [{**_DEP, "reff": "x"}]}, r"\[\[deps\]\]\[0\]\.reff"),
        ({"package": _PACKAGE, "downstream-gate": {"label": "x", "labels": []}}, r"\[downstream-gate\]\.labels"),
    ],
)
def test_unknown_keys_are_rejected(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestSchemaError, match=match):
        validate(data)


def test_json_schema_uses_toml_keys() -> None:
    props = ManifestFile.model_json_schema(by_alias=True)["$defs"]["DepTable"]["properties"]
    assert "compiler-inputs" in props
    assert "needs-python" in props
