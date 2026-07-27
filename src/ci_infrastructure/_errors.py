"""Shared CLI error type.

A single user-facing failure exception. click catches it at the command
boundary, calls show(), and exits with exit_code (1) — so our own code raises
instead of calling sys.exit or returning integer status codes.
"""

from __future__ import annotations

import sys
from typing import IO

import click


class CIError(click.ClickException):
    """A user-facing CI failure, rendered as a GitHub Actions ::error:: annotation.

    Raising this is the only error path we need: click turns it into the single
    process-level exit at the command boundary. For a multi-line message GitHub
    parses the workflow command only on the first line, so the headline is
    annotated and any following lines show as plain log.
    """

    def show(self, file: IO[str] | None = None) -> None:
        click.echo(f"::error::{self.format_message()}", file=sys.stderr)
