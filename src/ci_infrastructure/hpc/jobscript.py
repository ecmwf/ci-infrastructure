# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Render the SLURM job script from a repo's ``.ci/hpc/build-<toolchain>.sh[.j2]`` recipe.

The runner submits first, then ships the source and touches ``TRANSFER_COMPLETED``;
the job waits for that marker, unpacks into node-local ``$TMPDIR`` and builds.
"""

from __future__ import annotations

import functools
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final

import jinja2
import jinja2.meta
import jinja2.nodes

from .._github_api import Execution

SENTINEL_SUCCESS: Final = "Finished: SUCCESS"
SENTINEL_FAILURE: Final = "Finished: FAILURE"

#: Appended to every sentinel so a verdict names the job that wrote it.
SENTINEL_JOB_ID: Final = "${SLURM_JOB_ID:-unknown}"


def sentinel_echo(sentinel: str) -> str:
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
    """The cross-runner reattach key."""
    return f"ci-{artifact_name}"


JOB_TEMPLATE_SUFFIX: Final = ".j2"

_CONTEXT_EXTRAS: Final = ("leg", "artifact_name", "execution", "ci_packages", "ci_dep_shas", "ci_crates", "release")

#: Leg keys the pipeline adds (``_resolved`` by resolve-deps, ``_release`` by the release gate): not leg fields.
_INTERNAL_KEY_PREFIX: Final = "_"

#: The release leg's context, under ``release`` in the template.
RELEASE_KEY: Final = "_release"

#: Every release overlay template emits this line; a recipe that bypasses the overlay renders without it.
RELEASE_OVERLAY_MARKER: Final = "# ci-infrastructure: release overlay"

#: E.g. ``{% extends "ci-infrastructure/cmake-atos.sh.j2" %}``.
BASE_TEMPLATE_PREFIX: Final = "ci-infrastructure"

#: The release overlay's blocks a repo recipe may still define in a release: its CMake flags (re-declared
#: inside the overlay's configure), and how an image without rpmbuild gets it. Every other block an
#: overlay template defines is release-owned (:func:`release_owned_blocks`).
RELEASE_RECIPE_BLOCKS: Final = frozenset({"cmake_args", "packaging_tools"})

#: Runner legs name their compilers in the compiler-input fields; the recipes read these.
#: The binary a recipe calls: the leg's `*-compiler-binary`, else the compiler that identifies the
#: build, which is the same binary wherever no module renames it.
_COMPILER_BINARIES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "c_compiler_binary": "c_compiler",
        "cxx_compiler_binary": "cxx_compiler",
        "fortran_compiler_binary": "fortran_compiler",
    }
)

#: Optional knobs shared by every lane; a leg's own value wins. Lane-specific ones are
#: defaulted in their template with `| default(...)`.
JOB_TEMPLATE_DEFAULTS: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "tests": True,
        "ctest_args": "",
        "fortran_compiler_binary": "",
        "options": "",
        "python_version": "",
        "modules": [],
        # Rust bindings: crates a publish leaves out when the registry has their version already.
        "cargo_skip_published": [],
    }
)


class JobTemplateError(Exception):
    """A `.j2` recipe that cannot be rendered for the leg that selected it."""


def is_job_template(path: str | Path) -> bool:
    return str(path).endswith(JOB_TEMPLATE_SUFFIX)


def template_var(field: str) -> str:
    """``cxx-compiler`` -> ``cxx_compiler``."""
    return field.replace("-", "_")


def _is_leg_field(key: str) -> bool:
    return not key.startswith(_INTERNAL_KEY_PREFIX)


def is_release_leg(leg: Mapping[str, Any]) -> bool:
    return RELEASE_KEY in leg


def job_template_environment(search_path: Path | None = None, *, release: bool = False) -> jinja2.Environment:
    """``release``: ``templates/release`` shadows the shared templates, i.e. its cmake-all-lanes.sh.j2."""
    shared: jinja2.BaseLoader = jinja2.PackageLoader("ci_infrastructure", "templates")
    if release:
        shared = jinja2.ChoiceLoader([jinja2.PackageLoader("ci_infrastructure", "templates/release"), shared])
    # The shared templates come first, so a repo file cannot shadow them.
    loaders: list[jinja2.BaseLoader] = [jinja2.PrefixLoader({BASE_TEMPLATE_PREFIX: shared})]
    if search_path:
        loaders.append(jinja2.FileSystemLoader(str(search_path)))
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(loaders),
        # A name the leg does not declare fails instead of rendering empty.
        undefined=jinja2.StrictUndefined,
        # Shell, not markup; quote per use with the `quote` (alias `sh`) filter.
        autoescape=False,
        # Keeps a templated #SBATCH block contiguous for _split_header.
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        newline_sequence="\n",
    )
    # For a single shell word only; a list of flags must stay unquoted.
    env.filters["quote"] = env.filters["sh"] = shlex.quote
    return env


@functools.cache
def release_owned_blocks() -> frozenset[str]:
    """Every block a release overlay template (``templates/release/``) defines, but :data:`RELEASE_RECIPE_BLOCKS`.

    A repo recipe defining one would replace what the release does there (configure, install, publish, the
    HPC install test in ``verify``, ...), so a release render refuses it.
    """
    env = jinja2.Environment()
    loader = jinja2.PackageLoader("ci_infrastructure", "templates/release")
    names: set[str] = set()
    for name in loader.list_templates():
        source, _, _ = loader.get_source(env, name)
        names |= {b.name for b in env.parse(source, name=name).find_all(jinja2.nodes.Block)}
    return frozenset(names - RELEASE_RECIPE_BLOCKS)


def _is_base_template(env: jinja2.Environment, name: str) -> bool:
    """Whether ci-infrastructure's own loader serves ``name``: the name prefix alone would also match a repo file."""
    loader = env.loader
    if isinstance(loader, jinja2.ChoiceLoader):
        loader = loader.loaders[0]
    if not isinstance(loader, jinja2.PrefixLoader):
        return False
    try:
        loader.get_source(env, name)
    except jinja2.TemplateNotFound:
        return False
    return True


def repo_template_blocks(template_source: str, *, template_name: str, env: jinja2.Environment) -> dict[str, list[str]]:
    """Block name -> the repo templates defining it: the recipe and every repo template it references.

    From the parsed templates (nested blocks too), not their rendering. Every ``extends``, ``include``,
    ``import`` and ``from ... import`` is followed, wherever it sits (an included template may itself
    extend ci-infrastructure's and override its blocks); only ci-infrastructure's own templates are not.
    A template named by a computed expression is refused: it cannot be checked.
    """
    assert env.loader is not None
    out: dict[str, list[str]] = {}
    seen = {template_name}
    pending = [(template_source, template_name)]
    while pending:
        source, name = pending.pop()
        try:
            ast = env.parse(source, filename=name)
        except jinja2.TemplateSyntaxError as exc:
            raise JobTemplateError(f"{name}:{exc.lineno}: {exc.message}") from exc
        for block in ast.find_all(jinja2.nodes.Block):
            out.setdefault(block.name, []).append(name)
        for ref in jinja2.meta.find_referenced_templates(ast):
            if ref is None:
                raise JobTemplateError(
                    f"{name}: extends/include/import names a template by a computed expression, which a "
                    "release cannot check; name it literally"
                )
            if ref in seen or _is_base_template(env, ref):
                continue
            seen.add(ref)
            try:
                ref_source, _, _ = env.loader.get_source(env, ref)
            except jinja2.TemplateNotFound as exc:
                raise JobTemplateError(f"{name}: template {ref!r} not found") from exc
            pending.append((ref_source, ref))
    return out


def check_release_recipe(template_source: str, *, template_name: str, env: jinja2.Environment) -> None:
    """Refuse a repo recipe (or a repo template it extends, includes or imports) that defines a release-owned block."""
    blocks = repo_template_blocks(template_source, template_name=template_name, env=env)
    if owned := sorted(set(blocks) & release_owned_blocks()):
        where = "; ".join(f"{b!r} in {', '.join(sorted(set(blocks[b])))}" for b in owned)
        raise JobTemplateError(
            f"{template_name}: a release recipe defines the release-owned block(s) {where}. The release overlay "
            "owns them (its configure, install, publish and install test must run as written), so a release "
            f"recipe may define only {sorted(RELEASE_RECIPE_BLOCKS)}; move the rest into those, or out of the recipe"
        )


def build_template_context(leg: Mapping[str, Any], *, execution: Execution, artifact_name: str = "") -> dict[str, Any]:
    """``_resolved`` is left out: its runner-local paths are invalid on the cluster.

    ``_release`` becomes ``release`` (None for a CI leg); no other ``_`` key reaches the template.
    """
    context: dict[str, Any] = {}
    origin: dict[str, str] = {}
    for key, value in leg.items():
        if not _is_leg_field(key):
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
    if not isinstance(context.get("python_version", ""), str):
        raise JobTemplateError(
            f'python-version must be a string such as "3.10", not {context["python_version"]!r}: '
            f"unquoted, TOML reads 3.10 as the number 3.1"
        )
    for name, source in _COMPILER_BINARIES.items():
        if name not in context and source in context:
            context[name] = context[source]
    for name, value in JOB_TEMPLATE_DEFAULTS.items():
        context.setdefault(name, value)
    context["leg"] = dict(leg)
    context["artifact_name"] = artifact_name
    context["execution"] = execution
    # The packages the leg's kind publishes, in build order; each installs to $CI_INSTALL_ROOT/<name>.
    context["ci_packages"] = list((leg.get("_resolved") or {}).get("packages") or {})
    resolved = leg.get("_resolved") or {}
    # The commit resolve-deps built each dep's repo from, and the crates the manifest takes from it.
    context["ci_dep_shas"] = {d["repo"]: d["sha"] for d in resolved.get("deps") or []}
    context["ci_crates"] = dict(resolved.get("crates") or {})
    context["release"] = leg.get(RELEASE_KEY)
    return context


def declared_template_names(leg: Mapping[str, Any]) -> set[str]:
    names = {template_var(k) for k in leg if _is_leg_field(k)}
    names |= {name for name, source in _COMPILER_BINARIES.items() if source in names}
    return names | set(_CONTEXT_EXTRAS) | set(JOB_TEMPLATE_DEFAULTS)


def _assigned_outside_blocks(node: jinja2.nodes.Node) -> set[str]:
    """Names a template sets, imports or defines as a macro outside its blocks; Jinja runs that first."""
    names: set[str] = set()
    for child in node.iter_child_nodes():
        if isinstance(child, (jinja2.nodes.Assign, jinja2.nodes.AssignBlock)):
            target = child.target
            names |= (
                {target.name}
                if isinstance(target, jinja2.nodes.Name)
                else {n.name for n in target.find_all(jinja2.nodes.Name)}
            )
        elif isinstance(child, jinja2.nodes.Import):
            names.add(child.target)
        elif isinstance(child, jinja2.nodes.FromImport):
            names |= {n if isinstance(n, str) else n[1] for n in child.names}
        elif isinstance(child, jinja2.nodes.Macro):
            names.add(child.name)
        if not isinstance(child, (jinja2.nodes.Block, jinja2.nodes.Macro, jinja2.nodes.For, jinja2.nodes.With)):
            names |= _assigned_outside_blocks(child)
    return names


def undeclared_template_names(
    template_source: str,
    leg: Mapping[str, Any],
    *,
    template_name: str,
    search_path: Path | None = None,
) -> set[str]:
    """Names the template (or what it extends/includes) reads that neither the leg nor a ``| default`` provides.

    Static, so misses ``leg['x']``.
    """
    env = job_template_environment(search_path, release=is_release_leg(leg))
    assert env.loader is not None
    names: set[str] = set()
    defaulted: set[str] = set()
    assigned: set[str] = set()
    seen = {template_name}
    pending = [(template_source, template_name)]
    while pending:
        source, name = pending.pop()
        try:
            ast = env.parse(source, filename=name)
        except jinja2.TemplateSyntaxError as exc:
            raise JobTemplateError(f"{name}:{exc.lineno}: {exc.message}") from exc
        names |= jinja2.meta.find_undeclared_variables(ast)
        assigned |= _assigned_outside_blocks(ast)
        defaulted |= {
            f.node.name
            for f in ast.find_all(jinja2.nodes.Filter)
            if f.name == "default" and isinstance(f.node, jinja2.nodes.Name)
        }
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
    return names - defaulted - assigned - declared_template_names(leg)


def undeclared_names_message(recipe: str, missing: set[str], leg: Mapping[str, Any]) -> str:
    declared = sorted(k for k in leg if _is_leg_field(k))
    return (
        f"recipe '{recipe}' reads {sorted(missing)}, which this "
        f"leg does not declare. The leg has {declared}; a template may read those "
        f"(hyphens as underscores), plus `leg`, `artifact_name`, the defaults "
        f"{sorted(JOB_TEMPLATE_DEFAULTS)} and what a template reads through "
        f"`| default(...)`. Add the key to the "
        f"leg, or drop it from the recipe — they are meant to say the same thing."
        + "".join(
            f" `{name}` comes from `{name.removesuffix('_binary').replace('_', '-')}`, or "
            f"`{name.replace('_', '-')}` when the binary to call is named differently."
            for name in sorted(missing)
            if name.endswith("_compiler_binary")
        )
    )


def render_job_template(
    *,
    template_source: str,
    template_name: str,
    leg: Mapping[str, Any],
    execution: Execution,
    artifact_name: str = "",
    search_path: Path | None = None,
) -> str:
    # The check the workflow generator runs, so a recipe it rejects fails in the pull request already.
    if missing := undeclared_template_names(template_source, leg, template_name=template_name, search_path=search_path):
        raise JobTemplateError(undeclared_names_message(template_name, missing, leg))
    release = is_release_leg(leg)
    if release and not isinstance(leg[RELEASE_KEY], Mapping):
        raise JobTemplateError(f"{template_name}: the leg's {RELEASE_KEY} must be a table, got {leg[RELEASE_KEY]!r}")
    env = job_template_environment(search_path, release=release)
    if release:
        check_release_recipe(template_source, template_name=template_name, env=env)
    try:
        template = env.from_string(template_source)
        rendered = template.render(build_template_context(leg, execution=execution, artifact_name=artifact_name))
    except jinja2.TemplateSyntaxError as exc:
        raise JobTemplateError(f"{exc.name or template_name}:{exc.lineno}: {exc.message}") from exc
    except jinja2.TemplateNotFound as exc:
        raise JobTemplateError(f"{template_name}: template {exc.name!r} not found") from exc
    except jinja2.UndefinedError as exc:
        declared = sorted(k for k in leg if _is_leg_field(k))
        raise JobTemplateError(
            f"{template_name}: {exc.message}. The matrix leg declares {declared or '(nothing)'}; "
            f"a template may only read those (hyphens as underscores), plus `leg`, "
            f"`artifact_name`, `execution`, the defaults {sorted(JOB_TEMPLATE_DEFAULTS)} and what a template "
            f"reads through `| default(...)`. Add the key to the "
            f"leg in .ci/manifest.toml, or drop it from the recipe -- the two are meant to say the "
            f"same thing."
        ) from exc
    if not rendered.strip():
        raise JobTemplateError(
            f"{template_name} renders nothing for execution = {execution!r}; "
            "does a template it extends (e.g. cmake-all-lanes.sh.j2) have a branch for that lane?"
        )
    if release and RELEASE_OVERLAY_MARKER not in rendered.splitlines():
        raise JobTemplateError(
            f"{template_name} renders a release leg without the release overlay: a release recipe must "
            f'extend "{BASE_TEMPLATE_PREFIX}/cmake-all-lanes.sh.j2", which the overlay replaces; one that '
            "extends cmake-runner.sh.j2 or cmake-atos.sh.j2 directly would build it as CI does"
        )
    return rendered


def _split_header(repo_script: str) -> tuple[str, list[str], list[str]]:
    """(shebang, leading blank/``#`` block, body)."""
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
    packages: Sequence[str] = (),
    path_dirs: Sequence[str] = (),
) -> str:
    """`packages`: a kind that publishes several; each installs to `$CI_INSTALL_ROOT/<name>` and is archived here.
    `path_dirs`: what the deps declare in add-to-path, on the cluster."""
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
    # For dlopen (findlibs); linked code finds its deps by RPATH. Missing dirs are harmless.
    lib_dirs = ":".join(f"{p}/lib64:{p}/lib" for p in cmake_prefix_path.split(":") if p)
    if lib_dirs:
        out.append(f'export LD_LIBRARY_PATH="{lib_dirs}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"')
    if path_dirs:
        out.append(f'export PATH="{":".join(path_dirs)}:$PATH"')
    out.append(f'export CI_INSTALL_PREFIX="{install_path}"')
    out.append(f'export CI_INSTALL_ARCHIVE="{install_archive_path(install_path)}"')
    if packages:
        out.append(f'export CI_INSTALL_ROOT="{install_path}"')
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
    for name in packages:
        archive = install_archive_path(f"{install_path}/{name}")
        # .part + mv, so the fetcher only ever sees a complete archive.
        out.append(f'tar -cf - -C "$CI_INSTALL_ROOT/{name}" . | zstd -T0 -q -o "{archive}.part"')
        out.append(f'mv "{archive}.part" "{archive}"')
    out.append(sentinel_echo(SENTINEL_SUCCESS))
    return "\n".join(out) + "\n"
