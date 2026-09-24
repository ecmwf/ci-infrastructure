# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Reference pages for the composite actions and reusable workflows, read from their YAML.

``.. autoaction:: <name>`` renders ``actions/<name>/action.yml``; ``.. autoworkflow:: <path>``
renders the ``workflow_call`` interface of a reusable workflow, its description being the
comments between the licence header and ``on:``. ``:action:`<name>``` links to either.

Like autosummary, the build writes one page per action and per workflow in
``ghactions_workflows`` under ``reference/actions/{composite,workflows}/``.

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


def _header_comments(text: str) -> list[str]:
    """The comment lines after the SPDX header and before ``on:``, as paragraphs."""
    out: list[str] = []
    for line in text.split("\non:", 1)[0].splitlines():
        if line.startswith("#") and "SPDX-" not in line and line.strip() != "#":
            out.append(line[1:].strip())
        elif out and out[-1]:
            out.append("")
    return out


class AutoWorkflow(_Base):
    required_arguments = 1

    def run(self) -> list[nodes.Node]:
        rel = self.arguments[0]
        path = self._root() / rel
        text = path.read_text(encoding="utf-8")
        doc = yaml.load(text, Loader=yaml.BaseLoader)
        call = (doc.get("on") or {}).get("workflow_call") or {}
        source = str(path)
        description = _header_comments(text)
        if not description:
            logger.warning("%s: workflow has no header comment to describe it", source, location=self.get_location())
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
            *description,
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


def _write_pages(
    outdir: Path, title: str, intro: str, pages: dict[str, str], groups: list[tuple[str, str, list[str]]] | None = None
) -> None:
    """Make ``outdir`` hold exactly ``pages`` plus an index, rewriting only what changed."""
    outdir.mkdir(parents=True, exist_ok=True)
    index = [title, "=" * len(title), "", intro, ""]
    for heading, blurb, names in groups or [(None, None, list(pages))]:
        if heading:
            index += [heading, "-" * len(heading), "", blurb, ""]
        index += [".. toctree::", "   :maxdepth: 1", "", *(f"   {n}" for n in names), ""]
    wanted = {"index.rst": "\n".join(index).rstrip("\n") + "\n", **{f"{n}.rst": f"{d}\n" for n, d in pages.items()}}
    for stale in {p.name for p in outdir.glob("*.rst")} - wanted.keys():
        (outdir / stale).unlink()
    for name, content in wanted.items():
        path = outdir / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")


def _generate(app: Sphinx) -> None:
    root, ref, repo = Path(app.config.ghactions_root), app.config.ghactions_ref, app.config.ghactions_repo
    out = Path(app.srcdir) / "reference" / "actions"
    actions = sorted(p.parent.name for p in (root / "actions").glob("*/action.yml"))
    groups = app.config.ghactions_groups
    for n in sorted(set(actions).difference(*(names for _, _, names in groups))):
        logger.warning("action %r is in no ghactions_groups entry", n)
    _write_pages(
        out / "composite",
        "Composite actions",
        f"Called as ``{repo}/actions/<name>@{ref}``.",
        {n: f".. autoaction:: {n}" for n in actions},
        groups,
    )
    _write_pages(
        out / "workflows",
        "Reusable workflows",
        f"Called as the ``uses:`` of a job, ``{repo}/.github/workflows/<file>@{ref}``.",
        {Path(w).stem: f".. autoworkflow:: {w}" for w in app.config.ghactions_workflows},
    )


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_config_value("ghactions_repo", "", "env")
    app.add_config_value("ghactions_ref", "main", "env")
    app.add_config_value("ghactions_root", "", "env")
    app.add_config_value("ghactions_workflows", [], "env")
    app.add_config_value("ghactions_groups", [], "env")
    app.add_crossref_type("action", "action", indextemplate="pair: %s; action")
    app.add_directive("autoaction", AutoAction)
    app.add_directive("autoworkflow", AutoWorkflow)
    app.connect("builder-inited", _generate)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
