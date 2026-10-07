# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""``reference/templates.rst``: every shared job-script template, read from the package."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
import jinja2.meta
import jinja2.nodes
from sphinx.application import Sphinx

_TEMPLATES = Path("src") / "ci_infrastructure" / "templates"
_PREFIX = "ci-infrastructure"


def _purpose(source: str) -> str:
    """The first paragraph of the leading ``{# #}`` comment after the licence lines."""
    start, end = source.find("{#"), source.find("#}")
    if start < 0 or end < 0:
        return ""
    lines = [line.strip() for line in source[start + 2 : end].splitlines()]
    text = [line for line in lines if not line.startswith("SPDX-")]
    paragraph: list[str] = []
    for line in text:
        if not line and paragraph:
            break
        if line:
            paragraph.append(line)
    return re.sub(r"(?<!`)`([^`]+)`(?!`)", r"``\1``", " ".join(paragraph))


def _row(name: str, source: str, url: str) -> list[str]:
    ast = jinja2.Environment().parse(source)
    bases = sorted({ref for ref in jinja2.meta.find_referenced_templates(ast) if ref})
    blocks = sorted({block.name for block in ast.find_all(jinja2.nodes.Block)})
    return [
        f"`{_PREFIX}/{name} <{url}/{name}>`__",
        _purpose(source),
        ", ".join(f"``{base.removeprefix(_PREFIX + '/')}``" for base in bases) or "—",
        ", ".join(f"``{block}``" for block in blocks) or "—",
    ]


def _generate(app: Sphinx) -> None:
    folder = Path(app.config.ghactions_root) / _TEMPLATES
    url = f"https://github.com/{app.config.ghactions_repo}/blob/{app.config.ghactions_ref}/{_TEMPLATES.as_posix()}"
    rows = [_row(path.name, path.read_text(encoding="utf-8"), url) for path in sorted(folder.glob("*.j2"))]
    lines = [
        "Job-script templates",
        "====================",
        "",
        "Every template ``ci-infrastructure`` ships for a recipe to extend; each name links to its source.",
        "A recipe names it with the prefix,",
        'e.g. ``{% extends "ci-infrastructure/cmake-all-lanes.sh.j2" %}``; see :doc:`../configuring/job-scripts`.',
        "",
        ".. list-table::",
        "   :header-rows: 1",
        "   :widths: 25 45 15 15",
        "",
        "   * - template",
        "     - purpose",
        "     - extends",
        "     - blocks it defines",
    ]
    for row in rows:
        lines.append(f"   * - {row[0]}")
        lines.extend(f"     - {cell}" for cell in row[1:])
    out = Path(app.srcdir) / "reference" / "templates.rst"
    content = "\n".join(lines) + "\n"
    if not out.exists() or out.read_text(encoding="utf-8") != content:
        out.write_text(content, encoding="utf-8")


def setup(app: Sphinx) -> dict[str, Any]:
    app.connect("builder-inited", _generate)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
