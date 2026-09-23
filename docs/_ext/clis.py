# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""One reference page per ``[project.scripts]`` entry, written under ``reference/cli/``.

A click command renders through sphinx-click; any other script by its module docstring.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from ghactions import _write_pages
from sphinx.application import Sphinx


def _page(root: Path, prog: str, target: str) -> str:
    module, attr = target.split(":")
    source = root / "src" / Path(*module.split(".")).with_suffix(".py")
    if "import click" in source.read_text(encoding="utf-8"):
        return f".. click:: {module}:{attr}\n   :prog: {prog}\n   :nested: full"
    return f"{prog}\n{'=' * len(prog)}\n\n.. automodule:: {module}"


def _generate(app: Sphinx) -> None:
    root = Path(app.config.ghactions_root)
    scripts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    _write_pages(
        Path(app.srcdir) / "reference" / "cli",
        "Command-line tools",
        "The actions call these; run them by hand to debug a step locally.",
        {prog: _page(root, prog, target) for prog, target in scripts.items()},
    )


def setup(app: Sphinx) -> dict[str, Any]:
    app.connect("builder-inited", _generate)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
