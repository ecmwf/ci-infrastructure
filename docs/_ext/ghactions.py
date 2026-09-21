# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Reference pages for the composite actions and reusable workflows, read from their YAML.

``.. autoaction:: <name>`` renders ``actions/<name>/action.yml``; ``.. autoactions::``
renders every action; ``.. autoworkflow:: <path>`` renders the ``workflow_call``
interface of a reusable workflow, its directive content standing in for the
description a workflow file cannot carry. ``:action:`<name>``` links to an action.

A missing description is a warning, so the ``-W`` docs build fails on it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import yaml
from docutils import nodes
from docutils.statemachine import StringList
from sphinx.application import Sphinx
from sphinx.util import logging
from sphinx.util.docutils import SphinxDirective
from sphinx.util.nodes import nested_parse_with_titles

logger = logging.getLogger(__name__)


def _cell(text: str) -> str:
    return " ".join(text.split())


def _table(title: str, header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    out = [f".. list-table:: {title}", "   :header-rows: 1", ""]
    for row in [header, *rows]:
        out.append(f"   * - {row[0]}")
        out.extend(f"     - {c}" for c in row[1:])
    return [*out, ""]


def _literal(value: Any) -> str:
    return f"``{value}``" if value not in (None, "") else ""


class _Base(SphinxDirective):
    def _root(self) -> Path:
        return Path(self.config.ghactions_root)

    def _warn_undescribed(self, source: str, what: str, spec: dict[str, Any]) -> None:
        for name, body in spec.items():
            if not (body or {}).get("description", "").strip():
                logger.warning("%s: %s %r has no description", source, what, name, location=self.get_location())

    def _io_tables(self, source: str, inputs: dict[str, Any], outputs: dict[str, Any], typed: bool) -> list[str]:
        self._warn_undescribed(source, "input", inputs)
        self._warn_undescribed(source, "output", outputs)
        header = ["Input", *(["Type"] if typed else []), "Required", "Default", "Description"]
        rows = [
            [
                f"``{name}``",
                *([_literal(b.get("type"))] if typed else []),
                "yes" if b.get("required") else "",
                _literal(b.get("default")),
                _cell(b.get("description", "")),
            ]
            for name, b in inputs.items()
        ]
        out = _table("Inputs", header, rows)
        out += _table(
            "Outputs",
            ["Output", "Description"],
            [[f"``{n}``", _cell(b.get("description", ""))] for n, b in outputs.items()],
        )
        return out

    def _usage(self, uses: str, inputs: dict[str, Any]) -> list[str]:
        required = [n for n, b in inputs.items() if b.get("required")]
        lines = [f"- uses: {uses}@{self.config.ghactions_ref}"]
        if required:
            lines += ["  with:", *(f"    {n}: ..." for n in required)]
        return [".. code-block:: yaml", "", *(f"   {line}" for line in lines), ""]

    def _render(self, rst: list[str], source: str) -> list[nodes.Node]:
        self.state.document.settings.record_dependencies.add(source)
        node = nodes.section()
        node.document = self.state.document
        nested_parse_with_titles(self.state, StringList(rst, source=source), node)
        return node.children


class AutoAction(_Base):
    required_arguments = 1

    def run(self) -> list[nodes.Node]:
        return self._render(*self.action_rst(self.arguments[0]))

    def action_rst(self, name: str) -> tuple[list[str], str]:
        path = self._root() / "actions" / name / "action.yml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = str(path)
        if not doc.get("description", "").strip():
            logger.warning("%s: action has no description", source, location=self.get_location())
        inputs, outputs = doc.get("inputs") or {}, doc.get("outputs") or {}
        rst = [
            f".. action:: {name}",
            "",
            f"``{name}``",
            "-" * (len(name) + 4),
            "",
            f"*{doc.get('name', name)}*",
            "",
            *textwrap.dedent(doc.get("description", "")).splitlines(),
            "",
            *self._usage(f"{self.config.ghactions_repo}/actions/{name}", inputs),
            *self._io_tables(source, inputs, outputs, typed=False),
        ]
        return rst, source


class AutoActions(AutoAction):
    required_arguments = 0

    def run(self) -> list[nodes.Node]:
        out: list[nodes.Node] = []
        for path in sorted((self._root() / "actions").glob("*/action.yml")):
            out += self._render(*self.action_rst(path.parent.name))
        return out


class AutoWorkflow(_Base):
    required_arguments = 1
    has_content = True

    def run(self) -> list[nodes.Node]:
        rel = self.arguments[0]
        path = self._root() / rel
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        call = (doc.get("on") or {}).get("workflow_call") or {}
        source = str(path)
        if not self.content:
            logger.warning("%s: autoworkflow needs a description as its content", source, location=self.get_location())
        name = Path(rel).name
        inputs = {k: {**v, "required": v.get("required") == "true"} for k, v in (call.get("inputs") or {}).items()}
        secrets = call.get("secrets") or {}
        self._warn_undescribed(source, "secret", secrets)
        rst = [
            f".. action:: {name}",
            "",
            f"``{name}``",
            "-" * (len(name) + 4),
            "",
            f"*{doc.get('name', name)}*",
            "",
            *self.content,
            "",
            ".. code-block:: yaml",
            "",
            f"   uses: {self.config.ghactions_repo}/{rel}@{self.config.ghactions_ref}",
            "",
            *self._io_tables(source, inputs, call.get("outputs") or {}, typed=True),
            *_table(
                "Secrets",
                ["Secret", "Description"],
                [[f"``{n}``", _cell(b.get("description", ""))] for n, b in secrets.items()],
            ),
        ]
        return self._render(rst, source)


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_config_value("ghactions_repo", "", "env")
    app.add_config_value("ghactions_ref", "main", "env")
    app.add_config_value("ghactions_root", "", "env")
    app.add_crossref_type("action", "action", indextemplate="pair: %s; action")
    app.add_directive("autoaction", AutoAction)
    app.add_directive("autoactions", AutoActions)
    app.add_directive("autoworkflow", AutoWorkflow)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
