# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""``.. manifest-schema::``: one section per `.ci/manifest.toml` table, from the pydantic models' JSON schema."""

from __future__ import annotations

import inspect
import json
from typing import Any

from docutils import nodes
from docutils.statemachine import StringList
from sphinx.application import Sphinx
from sphinx.util.docutils import SphinxDirective
from sphinx.util.nodes import nested_parse_with_titles

from ci_infrastructure import manifest

# (TOML header, model), in the order a manifest usually reads.
_TABLES = [
    ("[package]", manifest.PackageTable),
    ("[[deps]]", manifest.DepTable),
    ("[matrix.<kind>]", manifest.MatrixKindTable),
    ("[[trigger-downstream]]", manifest.TriggerDownstreamTable),
    ("[downstream-gate]", manifest.DownstreamGateTable),
    ("[generated]", manifest.GeneratedTable),
]


def _type(s: dict[str, Any]) -> str:
    if "const" in s:
        return json.dumps(s["const"])
    if "enum" in s:
        return " | ".join(json.dumps(v) for v in s["enum"])
    if "anyOf" in s:
        return " | ".join(_type(x) for x in s["anyOf"] if x.get("type") != "null")
    t = s.get("type")
    if t == "array":
        return f"array of {_type(s.get('items', {}))}" if s.get("items") else "array"
    if t == "object":
        extra = s.get("additionalProperties")
        return f"table of {_type(extra)}" if isinstance(extra, dict) and extra else "table"
    return {"boolean": "bool", "integer": "int"}.get(t, t or "any")


def _default(s: dict[str, Any]) -> str:
    if "default" not in s or s["default"] is None:
        return ""
    # TOML and JSON spell these scalars and arrays alike.
    return f"``{json.dumps(s['default'])}``"


def _table_rst(header: str, model: type[manifest._Table]) -> list[str]:
    schema = model.model_json_schema(by_alias=True)
    required = set(schema.get("required", ()))
    rst = [f"``{header}``", "-" * (len(header) + 4), "", *inspect.cleandoc(model.__doc__ or "").splitlines(), ""]
    rst += [".. list-table::", "   :header-rows: 1", "   :widths: 20 20 10 15 35", ""]
    rows = [["Key", "Type", "Required", "Default", "Description"]]
    for key, prop in schema["properties"].items():
        rows.append(
            [f"``{key}``", f"``{_type(prop)}``", "yes" if key in required else "", _default(prop), prop["description"]]
        )
    for row in rows:
        rst.append(f"   * - {row[0]}")
        rst.extend(f"     - {' '.join(c.split())}" for c in row[1:])
    return [*rst, ""]


class ManifestSchema(SphinxDirective):
    def run(self) -> list[nodes.Node]:
        self.state.document.settings.record_dependencies.add(manifest.__file__)
        rst: list[str] = []
        for header, model in _TABLES:
            rst += _table_rst(header, model)
        node = nodes.section()
        node.document = self.state.document
        nested_parse_with_titles(self.state, StringList(rst, source=manifest.__file__), node)
        return node.children


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_directive("manifest-schema", ManifestSchema)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
