# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CLI error type: raise it instead of calling sys.exit."""

from __future__ import annotations

import sys
from typing import IO

import click


class CIError(click.ClickException):
    """A user-facing CI failure, rendered as an ::error:: annotation (first line only)."""

    def show(self, file: IO[str] | None = None) -> None:
        click.echo(f"::error::{self.format_message()}", file=sys.stderr)
