# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering the SLURM job script from a repo's ``.ci/hpc/build-<toolchain>.sh[.j2]`` recipe.

The recipe keeps its leading ``#SBATCH`` block; the wrapper adds the output path,
the dependency environment and the verdict sentinel. Flow is submit-then-poll:
the runner submits first, then ships the source and touches ``TRANSFER_COMPLETED``
in the per-artifact staging dir; the job waits for that marker, unpacks into
node-local ``$TMPDIR`` and builds. A ``.j2`` recipe is rendered against its matrix
leg first, so the manifest and the script cannot disagree.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final

import jinja2
import jinja2.meta

SENTINEL_SUCCESS: Final = "Finished: SUCCESS"
SENTINEL_FAILURE: Final = "Finished: FAILURE"

#: Appended to every sentinel so a verdict names the job that wrote it.
SENTINEL_JOB_ID: Final = "${SLURM_JOB_ID:-unknown}"


def sentinel_echo(sentinel: str) -> str:
    """The shell command a job runs to publish ``sentinel`` as its verdict."""
    return f'echo "{sentinel} {SENTINEL_JOB_ID}"'


def sentinel_regex(jid: int | str) -> str:
    """Anchored ERE matching either sentinel, but only as written by job ``jid``."""
    return f"^({SENTINEL_SUCCESS}|{SENTINEL_FAILURE}) {jid}$"


DEFAULT_MARKER_WAIT_TIMEOUT: Final = 1800

#: Fixed names (not per run), so a reattaching runner can check and re-drop them.
SOURCE_TARBALL_NAME: Final = "source.tgz"
TRANSFER_MARKER_NAME: Final = "TRANSFER_COMPLETED"

INSTALL_ARCHIVE_SUFFIX: Final = "install.tar.zst"


def install_archive_path(install_path: str) -> str:
    """``CI_INSTALL_ARCHIVE``: where the job must write its install archive, atomically."""
    p = PurePosixPath(install_path)
    return str(p.parent / f"{p.name}.{INSTALL_ARCHIVE_SUFFIX}")


def job_name_for(artifact_name: str) -> str:
    """SLURM job name for an artifact: the cross-runner reattach key."""
    return f"ci-{artifact_name}"


#: Recipes with this suffix are rendered with Jinja; others are used verbatim.
JOB_TEMPLATE_SUFFIX: Final = ".j2"

#: Names the context supplies on top of the leg's own fields.
_CONTEXT_EXTRAS: Final = ("leg", "artifact_name")

#: Prefix under which ci-infrastructure's own recipes load, e.g.
#: ``{% extends "ci-infrastructure/cmake-build.sh.j2" %}``.
BASE_TEMPLATE_PREFIX: Final = "ci-infrastructure"

#: Names a recipe may read without its leg declaring them: the optional knobs of the
#: shared base template. A leg's own value always wins.
JOB_TEMPLATE_DEFAULTS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "time": "01:00:00",
        "ntasks": 8,
        "ssdtmp": "20G",
        "tests": True,
        "ctest_args": "",
        "fc": "",
        "options": "",
    }
)


class JobTemplateError(Exception):
    """A `.j2` recipe that cannot be rendered for the leg that selected it."""


def is_job_template(path: str | Path) -> bool:
    """Whether this job-script is rendered rather than read verbatim."""
    return str(path).endswith(JOB_TEMPLATE_SUFFIX)


def template_var(field: str) -> str:
    """``cxx-compiler`` -> ``cxx_compiler``: a leg key as a Jinja (and shell) name."""
    return field.replace("-", "_")


def job_template_environment(search_path: Path | None = None) -> jinja2.Environment:
    """The Jinja environment a `.j2` recipe is rendered in."""
    # The shared templates come first, so a repo file cannot shadow them.
    loaders: list[jinja2.BaseLoader] = [
        jinja2.PrefixLoader({BASE_TEMPLATE_PREFIX: jinja2.PackageLoader("ci_infrastructure.hpc", "templates")})
    ]
    if search_path:
        loaders.append(jinja2.FileSystemLoader(str(search_path)))
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(loaders),
        # A name the leg does not declare fails instead of rendering empty.
        undefined=jinja2.StrictUndefined,
        # Shell, not markup; quote per use with the `sh` filter.
        autoescape=False,
        # Keeps a templated #SBATCH block contiguous for _split_header.
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        newline_sequence="\n",
    )
    # For a single shell word only; a list of flags must stay unquoted.
    env.filters["sh"] = shlex.quote
    return env


def build_template_context(leg: Mapping[str, Any], *, artifact_name: str = "") -> dict[str, Any]:
    """The names a `.j2` recipe may reference: normalised leg keys, ``leg``, ``artifact_name`` and defaults.

    ``_resolved`` is absent: its runner-local paths are invalid on the cluster.
    """
    context: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for key, value in leg.items():
        if key == "_resolved":
            continue
        name = template_var(key)
        if name in _CONTEXT_EXTRAS:
            raise JobTemplateError(
                f"matrix leg field {key!r} normalises to {name!r}, which is a name the template "
                f"context already defines. Rename the manifest key."
            )
        if name in context:
            raise JobTemplateError(
                f"matrix leg fields {origin[name]!r} and {key!r} both normalise to {name!r}; one "
                f"would silently shadow the other. Rename one of the manifest keys."
            )
        origin[name] = key
        context[name] = value
    for name, value in JOB_TEMPLATE_DEFAULTS.items():
        context.setdefault(name, value)
    context["leg"] = dict(leg)
    context["artifact_name"] = artifact_name
    return context


def declared_template_names(leg: Mapping[str, Any]) -> set[str]:
    """Every top-level name a template may reference for this leg."""
    return {template_var(k) for k in leg if k != "_resolved"} | set(_CONTEXT_EXTRAS) | set(JOB_TEMPLATE_DEFAULTS)


def undeclared_template_names(
    template_source: str, leg: Mapping[str, Any], *, template_name: str, search_path: Path | None = None
) -> set[str]:
    """Names the template (or anything it extends/includes) reads that this leg does not supply.

    Static, so it also sees untaken branches, but not ``leg['x']``.
    """
    env = job_template_environment(search_path)
    assert env.loader is not None
    names: set[str] = set()
    seen = {template_name}
    pending = [(template_source, template_name)]
    while pending:
        source, name = pending.pop()
        try:
            ast = env.parse(source, filename=name)
        except jinja2.TemplateSyntaxError as exc:
            raise JobTemplateError(f"{name}:{exc.lineno}: {exc.message}") from exc
        names |= jinja2.meta.find_undeclared_variables(ast)
        for ref in jinja2.meta.find_referenced_templates(ast):
            if ref is None:
                raise JobTemplateError(
                    f"{name}: extends/include names a template by a computed expression, which cannot be "
                    f"checked; name it literally"
                )
            if ref in seen:
                continue
            seen.add(ref)
            try:
                ref_source, _, _ = env.loader.get_source(env, ref)
            except jinja2.TemplateNotFound as exc:
                raise JobTemplateError(f"{name}: template {ref!r} not found") from exc
            pending.append((ref_source, ref))
    return names - declared_template_names(leg)


def render_job_template(
    *,
    template_source: str,
    template_name: str,
    leg: Mapping[str, Any],
    artifact_name: str = "",
    search_path: Path | None = None,
) -> str:
    """Render a `.j2` recipe into the plain shell recipe render_job_script wraps."""
    env = job_template_environment(search_path)
    try:
        template = env.from_string(template_source)
        return template.render(build_template_context(leg, artifact_name=artifact_name))
    except jinja2.TemplateSyntaxError as exc:
        raise JobTemplateError(f"{exc.name or template_name}:{exc.lineno}: {exc.message}") from exc
    except jinja2.TemplateNotFound as exc:
        raise JobTemplateError(f"{template_name}: template {exc.name!r} not found") from exc
    except jinja2.UndefinedError as exc:
        declared = sorted(k for k in leg if k != "_resolved")
        raise JobTemplateError(
            f"{template_name}: {exc.message}. The matrix leg declares {declared or '(nothing)'}; "
            f"a template may only read those (hyphens as underscores), plus `leg`, "
            f"`artifact_name` and the defaults {sorted(JOB_TEMPLATE_DEFAULTS)}. Add the key to the "
            f"leg in .ci/manifest.toml, or drop it from the recipe -- the two are meant to say the "
            f"same thing."
        ) from exc


def _split_header(repo_script: str) -> tuple[str, list[str], list[str]]:
    """Split a build.sh into (shebang, leading blank/``#`` block, body); leading blank lines are skipped."""
    lines = repo_script.splitlines()
    shebang = ""
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start < len(lines) and lines[start].startswith("#!"):
        shebang = lines[start]
        start += 1
    header: list[str] = []
    body_start = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            header.append(lines[i])
        else:
            body_start = i
            break
    return shebang, header, lines[body_start:]


def _marker_wait_block(staging_dir: str, run_id: str, marker_wait_timeout: int) -> list[str]:
    """Bounded wait for the transfer marker, then unpack into node-local ``$CI_SOURCE_DIR``."""
    marker = f"{staging_dir}/{TRANSFER_MARKER_NAME}"
    tarball = f"{staging_dir}/{SOURCE_TARBALL_NAME}"
    return [
        'echo "ci: waiting for source-transfer marker..."',
        f'_ci_marker="{marker}"',
        f"_ci_deadline=$(( $(date +%s) + {marker_wait_timeout} ))",
        'until [ -f "$_ci_marker" ]; do',
        '  if [ "$(date +%s)" -ge "$_ci_deadline" ]; then',
        f'    echo "ci: source-transfer marker never arrived within {marker_wait_timeout}s" >&2',
        f"    {sentinel_echo(SENTINEL_FAILURE)}",
        "    exit 1",
        "  fi",
        "  sleep 5",
        "done",
        f'export CI_SOURCE_DIR="${{TMPDIR:-/tmp}}/ci-src-{run_id}"',
        'mkdir -p "$CI_SOURCE_DIR"',
        f'tar -xzf "{tarball}" -C "$CI_SOURCE_DIR"',
        'cd "$CI_SOURCE_DIR"',
    ]


def render_job_script(
    *,
    repo_script: str,
    output_path: str,
    cmake_prefix_path: str,
    install_path: str,
    job_name: str | None = None,
    staging_dir: str | None = None,
    run_id: str | None = None,
    marker_wait_timeout: int = DEFAULT_MARKER_WAIT_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> str:
    """Wrap a repo's build.sh into the final submittable SLURM script."""
    shebang, header, body = _split_header(repo_script)

    out: list[str] = [shebang or "#!/bin/bash"]
    out.extend(header)
    out.append(f"#SBATCH --output={output_path}")
    out.append(f"#SBATCH --error={output_path}")
    # The comment is provenance for humans only; see orchestrate.find_active_job_by_name.
    if job_name is not None:
        out.append(f"#SBATCH --job-name={job_name}")
    if run_id is not None:
        out.append(f"#SBATCH --comment={run_id}")
    out.append("")
    out.append("set -euo pipefail")
    out.append(f'export CMAKE_PREFIX_PATH="{cmake_prefix_path}${{CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}}"')
    out.append(f'export CI_INSTALL_PREFIX="{install_path}"')
    out.append(f'export CI_INSTALL_ARCHIVE="{install_archive_path(install_path)}"')
    for key, value in (env or {}).items():
        out.append(f'export {key}="{value}"')
    # Armed before the marker wait and body.
    out.append(f"_ci_on_err() {{ {sentinel_echo(SENTINEL_FAILURE)}; }}")
    out.append("trap _ci_on_err ERR")
    out.append("")
    if staging_dir is not None and run_id is not None:
        out.extend(_marker_wait_block(staging_dir, run_id, marker_wait_timeout))
        out.append("")
    out.extend(body)
    out.append("")
    out.append(sentinel_echo(SENTINEL_SUCCESS))
    return "\n".join(out) + "\n"
