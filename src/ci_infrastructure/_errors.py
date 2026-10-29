# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CLI error type: raise it instead of calling sys.exit."""

from __future__ import annotations

import sys
from typing import IO

import click


def escape_command_data(message: str) -> str:
    """A workflow command's message, all its lines kept in the annotation."""
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class CIError(click.ClickException):
    """A user-facing CI failure, rendered as an ::error:: annotation."""

    def show(self, file: IO[str] | None = None) -> None:
        click.echo(f"::error::{escape_command_data(self.format_message())}", file=sys.stderr)
