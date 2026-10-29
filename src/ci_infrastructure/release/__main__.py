# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""``ci-infrastructure-release``: the commands the release actions run."""

from __future__ import annotations

import click

from . import gate, nexus, source_deps


@click.group(help=__doc__)
def main() -> None:
    pass


main.add_command(gate.main)
main.add_command(source_deps.main)
main.add_command(nexus.main)


if __name__ == "__main__":
    main()
