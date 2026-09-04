# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Rendering the SLURM job script from a repo's build recipe.

The repo owns ``.ci/hpc/build-<toolchain>.sh`` — its ``#SBATCH`` resource directives,
``module load`` lines and its cmake/ctest body. ci-infrastructure wraps that
body with only the orchestration bits it must control:

  * ``#SBATCH --output/--error`` pointing at the path we tail for completion,
  * the dependency environment (``CMAKE_PREFIX_PATH``, ``CI_INSTALL_PREFIX``),
  * a **submit-then-poll** preamble: the job blocks until the runner drops the
    ``TRANSFER_COMPLETED`` marker in the shared staging dir (the runner submits
    first, then scp's the source tarball and touches the marker), then
    unpacks the checkout into node-local ``$TMPDIR`` and ``cd``s there,
  * a ``Finished: SUCCESS`` / ``Finished: FAILURE`` sentinel so the poller can
    tell the outcome (a completed SLURM job vanishes from ``squeue``, so the
    sentinel — not the scheduler — is the authoritative success signal).

The repo's leading directive block is preserved verbatim at the top so its
``#SBATCH`` header stays where SLURM requires it (before any command).

A recipe named ``.j2`` is first rendered as a Jinja template against the matrix
leg that selected it (``render_job_template``), and the result is wrapped exactly
as a hand-written ``.sh`` would be. That is what stops the manifest and the recipe
disagreeing: the compiler, the module set and the build type the artifact name is
built from become the same values the script runs with, instead of two independent
statements of the same fact.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final

import jinja2
import jinja2.meta

SENTINEL_SUCCESS: Final = "Finished: SUCCESS"
SENTINEL_FAILURE: Final = "Finished: FAILURE"

#: Default seconds the job waits for the source-transfer marker before giving up.
DEFAULT_MARKER_WAIT_TIMEOUT: Final = 1800

#: Fixed name of the source tarball the runner scp's into the staging dir and
#: the job unpacks. Independent of the run id so a reattach can re-drop it.
SOURCE_TARBALL_NAME: Final = "source.tgz"

#: Fixed name of the source-transfer marker, for the same reason as the tarball:
#: the staging dir is already per-artifact, so a reattaching runner can check and
#: re-drop it without knowing which run submitted the job it is adopting.
#: (Why not the scheduler's `Comment`: see orchestrate.find_active_job_by_name.)
TRANSFER_MARKER_NAME: Final = "TRANSFER_COMPLETED"

#: Suffix of the archive the install tree travels home in. Shared with
#: transfer.fetch_install so the job and the fetcher agree on one name. zstd
#: rather than gzip: it compresses on all the job's cores and the smaller result
#: is quicker to pull back over the connection.
INSTALL_ARCHIVE_SUFFIX: Final = "install.tar.zst"


def install_archive_path(install_path: str) -> str:
    """Where a job may leave its own install tarball, as ``CI_INSTALL_ARCHIVE``.

    Writing it lets a job that installs onto node-local disk hand the fetcher a
    finished archive, so the tree never has to be created on -- and then read
    back off -- the shared filesystem. It must be moved into place atomically:
    the fetcher takes any file at this path as complete.
    """
    p = PurePosixPath(install_path)
    return str(p.parent / f"{p.name}.{INSTALL_ARCHIVE_SUFFIX}")


def job_name_for(artifact_name: str) -> str:
    """SLURM job name for an artifact — the key a re-run reattaches by.

    The scheduler is the shared, cross-runner job store, and this name is the
    whole of the key: a second run finds the in-flight job by name instead of
    submitting a duplicate (the runner-local jid file this replaces was invisible
    to other runners). Nothing else about the job is read back — see the
    ``--comment`` note in ``render_job_script``.

    Artifact names are the safe ``[A-Za-z0-9_.-]`` set and well within SLURM's
    name length, so they are used verbatim under a ``ci-`` namespace prefix.
    Should a cluster ever cap the name length, hash the artifact here
    (``ci-<sha256(artifact)[:32]>``); the full name would then have to go
    somewhere we own, not into the comment.
    """
    return f"ci-{artifact_name}"


#: Suffix that opts a recipe into Jinja rendering. Anything else is read and
#: wrapped verbatim, exactly as before this existed -- so `.j2` is the whole of
#: the opt-in and a plain `.sh` stays a file you can run by hand on a login node.
JOB_TEMPLATE_SUFFIX: Final = ".j2"

#: Names the context supplies on top of the leg's own fields.
_CONTEXT_EXTRAS: Final = ("leg", "artifact_name")


class JobTemplateError(Exception):
    """A `.j2` recipe that cannot be rendered for the leg that selected it."""


def is_job_template(path: str | Path) -> bool:
    """Whether this job-script is rendered rather than read verbatim."""
    return str(path).endswith(JOB_TEMPLATE_SUFFIX)


def template_var(field: str) -> str:
    """``cxx-compiler`` -> ``cxx_compiler``: a leg key as a Jinja name.

    Jinja parses ``{{ cxx-compiler }}`` as a subtraction, so hyphenated manifest
    keys need a normalised spelling. Same transformation as
    ``generate_downstream_ci._field_to_var`` applies for the Decode step, so one
    manifest key is spelled one way wherever a human reads it.
    """
    return field.replace("-", "_")


def job_template_environment(search_path: Path | None = None) -> jinja2.Environment:
    """The Jinja environment a `.j2` recipe is rendered in."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(search_path)) if search_path else jinja2.BaseLoader(),
        # THE point of the feature. A template reading {{ modules }} on a leg that
        # declares none fails here, naming the leg -- instead of rendering an empty
        # `module load` line and building a different binary under an artifact name
        # that still claims the old toolchain.
        undefined=jinja2.StrictUndefined,
        # Off, deliberately, and not via select_autoescape: this is shell, not
        # markup. Escaping would turn every `&&`, `>` and `'` in a recipe body into
        # an entity. The shell analogue is the `sh` filter below, which cannot be
        # global -- a #SBATCH directive is not shell, and `#SBATCH --time='00:40:00'`
        # is not a valid one -- so the template author applies it per site.
        autoescape=False,
        # Together these make a `{% ... %}`-only line vanish rather than leave a
        # blank, which is what keeps a templated #SBATCH block the contiguous run of
        # `#`-lines _split_header needs.
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        newline_sequence="\n",
    )
    # The ONLY quoting tool a template has, since autoescape is off. Apply it to a
    # leg value used as a single shell WORD. Do not apply it to a value that is a
    # list of flags (ecflow's ctest-args) or a `module` sub-command -- quoting those
    # makes them one argument and breaks them. That call is the recipe author's.
    env.filters["sh"] = shlex.quote
    return env


def build_template_context(leg: Mapping[str, Any], *, artifact_name: str = "") -> dict[str, Any]:
    """The names a `.j2` recipe may reference.

      * every leg key under its normalised name (``cxx-compiler`` -> ``cxx_compiler``)
        -- the idiom, and the only form the generate-time check can see;
      * ``leg``, the raw mapping, for a key whose manifest spelling must be used
        verbatim. It costs the generate-time check, because jinja2.meta cannot see
        through a subscript;
      * ``artifact_name``, the identity this job is building.

    `_resolved` is deliberately absent. It carries runner-local install paths and
    an un-rewritten `cmake-prefix-path`; the deps are re-shipped to the cluster and
    that value is recomputed there (orchestrate.plan_remote_prefixes) AFTER the leg
    is read. A template that baked it in would point the job at directories no
    compute node can see -- a silently wrong build rather than a crash. For the
    same reason CI_INSTALL_PREFIX / CMAKE_PREFIX_PATH / CI_SOURCE_DIR /
    CI_INSTALL_ARCHIVE are not names here: they are not knowable at render time, and
    a recipe keeps writing "$CI_INSTALL_PREFIX" exactly as it does today.
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
    context["leg"] = dict(leg)
    context["artifact_name"] = artifact_name
    return context


def declared_template_names(leg: Mapping[str, Any]) -> set[str]:
    """Every top-level name a template may reference for this leg."""
    return {template_var(k) for k in leg if k != "_resolved"} | set(_CONTEXT_EXTRAS)


def undeclared_template_names(template_source: str, leg: Mapping[str, Any], *, template_name: str) -> set[str]:
    """Names the template reads that this leg does not supply.

    Static: it sees names inside a branch that is never taken, which is the right
    side to err on when the whole point is that the manifest declares what a leg
    is. It cannot see through `leg['x']` -- the documented price of that escape
    hatch. Raises JobTemplateError if the template does not parse at all.
    """
    env = job_template_environment()
    try:
        ast = env.parse(template_source, filename=template_name)
    except jinja2.TemplateSyntaxError as exc:
        raise JobTemplateError(f"{template_name}:{exc.lineno}: {exc.message}") from exc
    return jinja2.meta.find_undeclared_variables(ast) - declared_template_names(leg)


def render_job_template(
    *,
    template_source: str,
    template_name: str,
    leg: Mapping[str, Any],
    artifact_name: str = "",
    search_path: Path | None = None,
) -> str:
    """Render a `.j2` recipe into the plain shell recipe render_job_script wraps.

    The output is exactly what a hand-written `.sh` would have been, so the two
    kinds of recipe converge before anything else looks at them.
    """
    env = job_template_environment(search_path)
    try:
        template = env.from_string(template_source)
        return template.render(build_template_context(leg, artifact_name=artifact_name))
    except jinja2.TemplateSyntaxError as exc:
        raise JobTemplateError(f"{template_name}:{exc.lineno}: {exc.message}") from exc
    except jinja2.UndefinedError as exc:
        declared = sorted(k for k in leg if k != "_resolved")
        raise JobTemplateError(
            f"{template_name}: {exc.message}. The matrix leg declares {declared or '(nothing)'}; "
            f"a template may only read those (hyphens as underscores), plus `leg` and "
            f"`artifact_name`. Add the key to the leg in .ci/manifest.toml, or drop it from the "
            f"recipe -- the two are meant to say the same thing."
        ) from exc


def _split_header(repo_script: str) -> tuple[str, list[str], list[str]]:
    """Split a build.sh into (shebang, leading-directive/comment block, body).

    The header is the run of leading lines that are blank or start with ``#``
    (shebang, comments, ``#SBATCH`` / ``#TROIKA`` directives) — i.e. everything
    up to the first executable line. ``#SBATCH`` must precede any command, so
    appending our own directives at the end of this block keeps them valid.

    Leading blank lines are skipped before looking for the shebang: a rendered
    template can open with the blank a leading ``{% set %}`` left behind, and the
    shebang is still the first real line. Without this it would be demoted into the
    comment block and the wrapper would inject a second one above it, leaving the
    real shebang inert.
    """
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
    """Emit the bounded wait for the source-transfer marker + node-local unpack.

    The runner submits the job first, then scp's the source tarball into
    ``staging_dir`` and finally ``touch``es ``TRANSFER_MARKER_NAME`` there. The
    job blocks here until that marker appears (so it never unpacks a half-copied
    tarball), then unpacks the checkout into ``$TMPDIR`` and ``cd``s there — the
    recipe then sees its sources as ``$CI_SOURCE_DIR``, node-local for a fast
    build. A marker that never arrives is a failure (an explicit sentinel, since
    the ``ERR`` trap does not fire on a bare ``exit``).

    The marker is named for the staging dir, not the run: any runner that finds
    this job can satisfy it. ``run_id`` still names ``CI_SOURCE_DIR`` so two runs
    unpacking on one node cannot collide — that value is always the local
    ``--run-id``, never anything read back from the scheduler.
    """
    marker = f"{staging_dir}/{TRANSFER_MARKER_NAME}"
    tarball = f"{staging_dir}/{SOURCE_TARBALL_NAME}"
    return [
        'echo "ci: waiting for source-transfer marker..."',
        f'_ci_marker="{marker}"',
        f"_ci_deadline=$(( $(date +%s) + {marker_wait_timeout} ))",
        'until [ -f "$_ci_marker" ]; do',
        '  if [ "$(date +%s)" -ge "$_ci_deadline" ]; then',
        f'    echo "ci: source-transfer marker never arrived within {marker_wait_timeout}s" >&2',
        f'    echo "{SENTINEL_FAILURE}"',
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
    """Wrap a repo's build.sh into the final submittable SLURM script.

    When ``staging_dir`` and ``run_id`` are given, the job waits for the
    runner's source-transfer marker, unpacks the checkout into node-local
    ``$TMPDIR`` and ``cd``s there before running the body — so the recipe sees
    its sources as ``$CI_SOURCE_DIR`` the same way the runner build sees
    ``$GITHUB_WORKSPACE``.
    """
    shebang, header, body = _split_header(repo_script)

    out: list[str] = [shebang or "#!/bin/bash"]
    out.extend(header)
    out.append(f"#SBATCH --output={output_path}")
    out.append(f"#SBATCH --error={output_path}")
    # The NAME makes the scheduler the shared job store: submit-wait finds an
    # in-flight job for this artifact by name and reattaches to it (see
    # orchestrate.find_active_job_by_name).
    #
    # The COMMENT is provenance only — which run submitted this job, for a human
    # reading `squeue`/`sacct`. It is deliberately never read back: it belongs to
    # the scheduler, and sites rewrite it. ECMWF's sbatch wrapper appends its own
    # fields, so a run id recovered from it comes back as
    # `<run>-<attempt>;Gres=gres/ssdtmp:20G;` — a string with a `/` in it, which
    # cannot name a marker or a tarball path.
    if job_name is not None:
        out.append(f"#SBATCH --job-name={job_name}")
    if run_id is not None:
        out.append(f"#SBATCH --comment={run_id}")
    out.append("")
    out.append("set -euo pipefail")
    # Prepend the resolved deps so the repo body's cmake finds them; keep any
    # pre-existing value on the tail.
    out.append(f'export CMAKE_PREFIX_PATH="{cmake_prefix_path}${{CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}}"')
    out.append(f'export CI_INSTALL_PREFIX="{install_path}"')
    out.append(f'export CI_INSTALL_ARCHIVE="{install_archive_path(install_path)}"')
    for key, value in (env or {}).items():
        out.append(f'export {key}="{value}"')
    # A failure anywhere below (set -e trips ERR) prints the failure sentinel
    # before the job exits non-zero, so the trap must be armed before the
    # marker-wait/unpack and the body run.
    out.append(f'_ci_on_err() {{ echo "{SENTINEL_FAILURE}"; }}')
    out.append("trap _ci_on_err ERR")
    out.append("")
    if staging_dir is not None and run_id is not None:
        out.extend(_marker_wait_block(staging_dir, run_id, marker_wait_timeout))
        out.append("")
    out.extend(body)
    out.append("")
    out.append(f'echo "{SENTINEL_SUCCESS}"')
    return "\n".join(out) + "\n"
