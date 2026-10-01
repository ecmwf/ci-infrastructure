# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Render a matrix leg's job-script into a standalone bash script.

CI runs exactly this script (actions/run-job-script); run it by hand to reproduce a
leg, e.g. inside the leg's container. Every variable of the environment contract
defaults to a path under the current directory and can be overridden from outside.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import click

from . import job_names
from ._errors import CIError
from ._github_api import EXECUTION_HPC_ATOS, EXECUTION_RUNNER, Execution
from .generate_downstream_ci import Manifest, SchemaError, parse_manifest
from .hpc import jobscript

_ENV_DEFAULTS: Final[Mapping[str, str]] = {
    "CI_SOURCE_DIR": "$PWD",
    "CI_BUILD_DIR": "$PWD/_ci/build",
    "CI_INSTALL_PREFIX": "$PWD/_ci/install",
    "CMAKE_PREFIX_PATH": "",
}


def job_title(m: Manifest, kind: str, leg: Mapping[str, Any]) -> str:
    """As in the Actions UI, e.g. ``cxxmath/build (clang++-18, Release)``."""
    suffix = job_names.name_suffix(leg, m.matrices[kind].legs, m.compiler_inputs)
    return f"{m.package_name}/{kind} ({suffix})"


def select_leg(m: Manifest, wanted: str) -> tuple[str, dict[str, Any]]:
    """By full title, or without the ``<package>/`` prefix."""
    titles = {job_title(m, kind, leg): (kind, leg) for kind, mk in m.matrices.items() for leg in mk.legs}
    for title, hit in titles.items():
        if wanted in (title, title.removeprefix(f"{m.package_name}/")):
            return hit
    raise CIError(f"no leg titled {wanted!r} in {m.path}; one of:\n  " + "\n  ".join(titles))


def render(
    *, job_script: Path, leg: Mapping[str, Any], execution: Execution, title: str = "", artifact_name: str = ""
) -> str:
    source = job_script.read_text()
    if jobscript.is_job_template(job_script):
        try:
            source = jobscript.render_job_template(
                template_source=source,
                template_name=str(job_script),
                leg=leg,
                artifact_name=artifact_name,
                search_path=job_script.parent,
            )
        except jobscript.JobTemplateError as exc:
            raise CIError(str(exc)) from exc
    return wrap(source, leg=leg, execution=execution, title=title)


def wrap(recipe: str, *, leg: Mapping[str, Any], execution: Execution, title: str = "") -> str:
    """Shebang and #SBATCH header first, so the script also stays a valid SLURM job."""
    shebang, header, body = jobscript._split_header(recipe)
    while header and not header[0].strip():
        header.pop(0)
    out = [shebang or "#!/bin/bash", *header]
    if title:
        out.append(f"# {title}")
    if container := leg.get("container"):
        out.append(f"# Container: {container}")
    out.append("set -euo pipefail")
    for var, default in _ENV_DEFAULTS.items():
        out.append(f'export {var}="${{{var}:-{default}}}"')
    if execution == EXECUTION_HPC_ATOS:
        archive = f"$CI_INSTALL_PREFIX.{jobscript.INSTALL_ARCHIVE_SUFFIX}"
        out.append(f'export CI_INSTALL_ARCHIVE="${{CI_INSTALL_ARCHIVE:-{archive}}}"')
    out.append("")
    out.extend(body)
    return "\n".join(out) + "\n"


def _from_manifest(manifest: Path, leg_title: str) -> tuple[Path, dict[str, Any], Execution, str]:
    try:
        m = parse_manifest(manifest)
    except SchemaError as exc:
        raise CIError(str(exc)) from exc
    kind, leg = select_leg(m, leg_title)
    spec = str(leg.get("job-script") or "")
    if not spec:
        raise CIError(f"[matrix.{kind}] of {manifest} builds through an action, not a job-script; nothing to render")
    return m.repo_root / spec.removeprefix("./"), leg, m.matrices[kind].execution, job_title(m, kind, leg)


def _list_titles(manifest: Path) -> Sequence[str]:
    m = parse_manifest(manifest)
    return [
        job_title(m, kind, leg) + ("" if leg.get("job-script") else "  (no job-script)")
        for kind, mk in m.matrices.items()
        for leg in mk.legs
    ]


@click.command(help=__doc__)
@click.option("--manifest", type=click.Path(path_type=Path), default=Path(".ci/manifest.toml"), show_default=True)
@click.option("--leg", "leg_title", help="Job title of the leg, as in the Actions UI, with or without '<package>/'.")
@click.option("--list", "list_legs", is_flag=True, help="List the job titles in --manifest and exit.")
@click.option("--matrix-leg", "matrix_leg", help="The leg as JSON, instead of --manifest/--leg (how CI calls it).")
@click.option("--job-script", "job_script", type=click.Path(path_type=Path), help="The recipe, with --matrix-leg.")
@click.option(
    "--execution",
    type=click.Choice([EXECUTION_RUNNER, EXECUTION_HPC_ATOS]),
    default=EXECUTION_RUNNER,
    show_default=True,
    help="The lane, with --matrix-leg; --leg takes it from the manifest.",
)
@click.option("--artifact-name", "artifact_name", default="", help="Value for the template's `artifact_name`.")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Write here (executable) instead of stdout.")
def main(
    manifest: Path,
    leg_title: str | None,
    list_legs: bool,
    matrix_leg: str | None,
    job_script: Path | None,
    execution: Execution,
    artifact_name: str,
    output: Path | None,
) -> None:
    if list_legs:
        click.echo("\n".join(_list_titles(manifest)))
        return
    if matrix_leg is not None:
        if job_script is None:
            raise click.UsageError("--matrix-leg needs --job-script")
        leg = json.loads(matrix_leg)
        if not isinstance(leg, dict):
            raise click.UsageError("--matrix-leg must be a JSON object")
        title = ""
    elif leg_title is not None:
        job_script, leg, execution, title = _from_manifest(manifest, leg_title)
    else:
        raise click.UsageError("pass --leg (or --list), or --matrix-leg with --job-script")
    if not job_script.is_file():
        raise CIError(f"job-script does not exist: {job_script}")
    rendered = render(job_script=job_script, leg=leg, execution=execution, title=title, artifact_name=artifact_name)
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.write_text(rendered)
        output.chmod(0o755)


if __name__ == "__main__":
    main()
