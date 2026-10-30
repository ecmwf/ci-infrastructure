# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Release legs: the manifest's `[matrix.release-*]` legs, validated, each with its ``_release`` context.

Two channels: ``rpm`` (built on a runner, published to Nexus) and ``hpc-module`` (built on Atos,
installed under ``<hpc root>/<package>/<version>/...``, by default a dry-run root in ``$SCRATCH``).

``_release`` is everything the release overlay templates read as ``release``; it is rendered
here, once, so the job script needs nothing but bash (and ``ci-infrastructure-render --release``
reproduces it offline). Release legs carry no ``_resolved``: their deps come from ``release-ref``.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from .._github_api import EXECUTION_HPC_ATOS, EXECUTION_RUNNER, Execution, ManifestSchemaError
from ..generate_downstream_ci import Manifest, SchemaError, parse_manifest
from ..manifest import ManifestFile, is_release_kind
from ..manifest import validate as validate_manifest
from . import ReleaseError
from .cpack import HEREDOC_DELIMITER, cpack_overrides, exclude_script, rpm_filename
from .detect import ReleaseEvent
from .version import MODE_RELEASE, PACKAGE_RELEASE

State = Literal["build", "already-published"]
STATE_BUILD: Final[State] = "build"
STATE_ALREADY_PUBLISHED: Final[State] = "already-published"

CHANNEL_RPM: Final = "rpm"
CHANNEL_HPC_MODULE: Final = "hpc-module"
#: Channels milestone 1 builds; each maps to its CPack generator ("" = none: an install tree).
CHANNELS: Final[Mapping[str, str]] = {CHANNEL_RPM: "RPM", CHANNEL_HPC_MODULE: ""}
#: The lane each channel builds in.
CHANNEL_EXECUTION: Final[Mapping[str, Execution]] = {
    CHANNEL_RPM: EXECUTION_RUNNER,
    CHANNEL_HPC_MODULE: EXECUTION_HPC_ATOS,
}

#: Release builds and their install-test images come from our registry only.
TRUSTED_REGISTRY: Final = "eccr.ecmwf.int/"
PRODUCTION_ROOT: Final = "/usr/local/apps"

#: Where HPC modules are installed unless the gate is given another root: a dry-run tree of the
#: troika user's scratch, never the production module tree. Expanded on the cluster.
DEFAULT_HPC_ROOT: Final = "$SCRATCH/cd-v3-dry-run"
#: An HPC leg's install-prefix starts with this; the rest is the leg's own (e.g. ``/GNU/8.5``).
HPC_PREFIX_HEAD: Final = "{root}/{package}/{version}"

BUILD_META_DELIMITER: Final = "CI_RELEASE_BUILD_META"

_ID_UNSAFE_RE: Final = re.compile(r"[^A-Za-z0-9._-]")
_PREFIX_COMPONENT_RE: Final = re.compile(r"^[A-Za-z0-9._+-]+$")
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")
#: An HPC root: absolute, or under a cluster variable the troika user's login shell sets; expanded there.
_HPC_ROOT_RE: Final = re.compile(r"^(?P<var>\$(?:SCRATCH|HOME|PERM|HPCPERM))?(?P<path>(?:/[A-Za-z0-9._-]+)+)$")
#: A leg's `modules` line, rendered as `module <line>` into the job script.
_MODULE_LINE_RE: Final = re.compile(r"^[A-Za-z0-9._+/=-]+(?: [A-Za-z0-9._+/=-]+)*$")
#: Paths the nightly gc sweeps, which a release must never be installed under.
_CI_WORK_COMPONENT: Final = "github-ci"


def sanitise_id(s: str) -> str:
    """``g++-11`` -> ``gxx-11``; anything else outside ``[A-Za-z0-9._-]`` -> ``_``."""
    return _ID_UNSAFE_RE.sub("_", s.replace("+", "x"))


def check_install_prefix(prefix: object, *, allow_production: bool = False) -> str:
    """Absolute, normalised, plain components, not ``/``; ``/usr/local/apps`` only with allow-production."""
    if not isinstance(prefix, str) or not prefix.startswith("/"):
        raise ReleaseError(f"install-prefix must be an absolute path, got {prefix!r}")
    p = PurePosixPath(prefix)
    parts = p.parts[1:]
    if not parts or str(p) != prefix or any(c in (".", "..") for c in parts):
        raise ReleaseError(f"install-prefix must be a normalised path below /, got {prefix!r}")
    bad = [c for c in parts if not _PREFIX_COMPONENT_RE.fullmatch(c)]
    if bad:
        raise ReleaseError(f"install-prefix component(s) {bad} outside [A-Za-z0-9._+-]: {prefix!r}")
    if not allow_production and (prefix == PRODUCTION_ROOT or prefix.startswith(PRODUCTION_ROOT + "/")):
        raise ReleaseError(f"install-prefix {prefix!r} is under {PRODUCTION_ROOT}, refused without allow-production")
    return prefix


def check_hpc_root(root: object, *, allow_production: bool = False) -> str:
    """Where HPC modules go: ``$SCRATCH/...`` (``$HOME``, ``$PERM``, ``$HPCPERM``) or an absolute path.

    Plain components only, never under the CI work tree (``github-ci``, swept nightly), and
    ``/usr/local/apps`` only with allow-production.
    """
    m = _HPC_ROOT_RE.fullmatch(root) if isinstance(root, str) else None
    if m is None:
        raise ReleaseError(
            f"HPC root {root!r} must be $SCRATCH/<dir>... ($HOME, $PERM or $HPCPERM too) or an absolute path, "
            "each component of [A-Za-z0-9._-]"
        )
    parts = m.group("path").split("/")[1:]
    if any(c in (".", "..") for c in parts):
        raise ReleaseError(f"HPC root {root!r} must be normalised (no . or .. components)")
    if _CI_WORK_COMPONENT in parts:
        raise ReleaseError(f"HPC root {root!r} is in the CI work tree ({_CI_WORK_COMPONENT}), which is swept nightly")
    if m.group("var") is None:
        check_install_prefix(m.group("path"), allow_production=allow_production)
    return str(root)


def hpc_prefix(template: object, *, root: str, package: str, version: str) -> str:
    """An HPC leg's install-prefix (``{root}/{package}/{version}[/...]``) for this release."""
    if not isinstance(template, str) or not template.startswith(HPC_PREFIX_HEAD):
        raise ReleaseError(f"an hpc-module install-prefix starts with {HPC_PREFIX_HEAD!r}, got {template!r}")
    rest = template.removeprefix(HPC_PREFIX_HEAD)
    tail = rest.split("/")[1:] if rest else []
    if (rest and not rest.startswith("/")) or any(c in ("", ".", "..") or not _TOKEN_RE.fullmatch(c) for c in tail):
        raise ReleaseError(
            f"install-prefix {template!r}: after {HPC_PREFIX_HEAD!r} only /<component>... of {_TOKEN_RE.pattern}"
        )
    for what, value in (("package", package), ("version", version)):
        if not _TOKEN_RE.fullmatch(value):
            raise ReleaseError(f"the HPC {what} {value!r} must match {_TOKEN_RE.pattern}")
    return f"{root}/{package}/{version}{rest}"


def prefix_shell_word(prefix: str) -> str:
    """``$SCRATCH/a/b`` -> ``"$SCRATCH"/a/b``: one shell word that expands the variable and nothing else."""
    m = _HPC_ROOT_RE.fullmatch(prefix)
    if m is not None and m.group("var"):
        return f'"{m.group("var")}"{shlex.quote(m.group("path"))}'
    return shlex.quote(prefix)


def _require_str(kind: str, leg: Mapping[str, Any], key: str) -> str:
    value = leg.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"[matrix.{kind}] leg {_describe(leg)} needs {key!r} (a non-empty string)")
    return value


def _require_token(kind: str, leg: Mapping[str, Any], key: str) -> str:
    value = _require_str(kind, leg, key)
    if not _TOKEN_RE.fullmatch(value):
        raise ReleaseError(f"[matrix.{kind}] {key} {value!r} must match {_TOKEN_RE.pattern}")
    return value


def _describe(leg: Mapping[str, Any]) -> str:
    return "(" + ", ".join(f"{k}={leg[k]}" for k in ("platform", "channel") if k in leg) + ")"


def validate_leg(kind: str, leg: Mapping[str, Any], *, execution: str, allow_production: bool = False) -> None:
    """The fields a release leg must set; what CI legs may leave to defaults, a release may not."""
    channel = _require_str(kind, leg, "channel")
    if channel not in CHANNELS:
        raise ReleaseError(f"[matrix.{kind}] channel {channel!r}: this release lane builds {sorted(CHANNELS)} only")
    lane = CHANNEL_EXECUTION[channel]
    if execution != lane:
        raise ReleaseError(f"[matrix.{kind}] execution {execution!r}: the {channel} channel builds on {lane!r}")
    spec = _require_str(kind, leg, "job-script")
    if not spec.endswith(".j2"):
        raise ReleaseError(f"[matrix.{kind}] job-script {spec!r} must be a .j2 recipe extending cmake-all-lanes")
    # Options name the CMake preset, as in the base template; a release names it explicitly.
    _require_str(kind, leg, "options")
    if leg.get("tests", True) is False:
        raise ReleaseError(f"[matrix.{kind}] a release leg runs its tests: drop tests = false")
    if not _require_str(kind, leg, "container").startswith(TRUSTED_REGISTRY):
        raise ReleaseError(f"[matrix.{kind}] container {leg['container']!r} is not from {TRUSTED_REGISTRY}")
    command = _require_str(kind, leg, "install-test-command")
    _require_str(kind, leg, "build-type")
    _require_token(kind, leg, "platform")
    if channel == CHANNEL_HPC_MODULE:
        _require_token(kind, leg, "site")
        modules = leg.get("modules", [])
        if not isinstance(modules, list) or not all(
            isinstance(m, str) and _MODULE_LINE_RE.fullmatch(m) for m in modules
        ):
            raise ReleaseError(
                f"[matrix.{kind}] modules {modules!r}: a list of `module` arguments, words of [A-Za-z0-9._+/=-]"
            )
        if command.startswith("/"):
            raise ReleaseError(
                f"[matrix.{kind}] install-test-command {command!r}: on HPC it runs in the unpacked prefix, "
                "so name the program relative to it (bin/...)"
            )
        hpc_prefix(leg.get("install-prefix"), root="/r", package="p", version="v")
        return
    if not _require_str(kind, leg, "install-test-image").startswith(TRUSTED_REGISTRY):
        raise ReleaseError(
            f"[matrix.{kind}] install-test-image {leg['install-test-image']!r} is not from {TRUSTED_REGISTRY}"
        )
    for key in ("arch", "nexus-target"):
        _require_token(kind, leg, key)
    check_install_prefix(leg.get("install-prefix"), allow_production=allow_production)


@dataclass(frozen=True)
class ReleaseManifest:
    raw: ManifestFile
    m: Manifest

    @property
    def package_name(self) -> str:
        assert self.raw.release is not None and self.raw.release.package_name is not None
        return self.raw.release.package_name


def load(manifest_path: Path) -> ReleaseManifest:
    try:
        with manifest_path.open("rb") as fh:
            raw = validate_manifest(tomllib.load(fh))
        m = parse_manifest(manifest_path)
    except (ManifestSchemaError, SchemaError, OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"{manifest_path}: {exc}") from exc
    if raw.release is None:
        raise ReleaseError(f"{manifest_path} has no [release] table")
    return ReleaseManifest(raw=raw, m=m)


def source_deps(rm: ReleaseManifest) -> list[dict[str, str]]:
    """The top-level `[[deps]]` with a string release-ref: one per package, built from source."""
    out: list[dict[str, str]] = []
    for d in rm.raw.deps:
        if not isinstance(d.release_ref, str):
            continue
        assert d.repo is not None  # the schema refuses release-ref without repo
        if d.compiler_inputs != ():
            raise ReleaseError(
                f"[[deps]] {d.repo}: release-ref {d.release_ref!r} on a compiled dep (compiler-inputs "
                f"{list(d.compiler_inputs) if d.compiler_inputs is not None else 'unset'}); only source deps "
                "(compiler-inputs = []) can be fetched for a release yet. Use release-ref = false for one the "
                "image provides"
            )
        out += [{"repo": d.repo, "package": p, "ref": d.release_ref} for p in d.packages]
    return out


def leg_id(kind: str, leg: Mapping[str, Any], compiler_inputs: Sequence[str]) -> str:
    parts = [
        kind.removeprefix("release-"),
        str(leg.get("platform", "")),
        *(str(leg.get(f, "")) for f in compiler_inputs),
    ]
    return sanitise_id("-".join(p for p in parts if p))


def job_key(package_name: str, tag_part: str, leg: str, sha: str) -> str:
    """Package, tag, leg and commit: a rerun of rc2 can never meet an rc1 build's paths or names."""
    return sanitise_id(f"{package_name}-{tag_part}-{leg}-{sha[:8]}")


def release_context(
    rm: ReleaseManifest,
    kind: str,
    leg: Mapping[str, Any],
    event: ReleaseEvent,
    *,
    allow_production: bool = False,
    state: State = STATE_BUILD,
    hpc_root: str = DEFAULT_HPC_ROOT,
) -> dict[str, Any]:
    mk = rm.m.matrices[kind]
    validate_leg(kind, leg, execution=mk.execution, allow_production=allow_production)
    meta = rm.raw.release
    assert meta is not None
    version = event.version
    channel = str(leg["channel"])
    generator = CHANNELS[channel]
    compiler_inputs = rm.m.packages[mk.packages[0]].compiler_inputs if mk.packages else rm.m.compiler_inputs
    lid = leg_id(kind, leg, compiler_inputs)
    ctx: dict[str, Any] = {
        "mode": version.mode,
        "tag": version.tag or "",
        "version": version.base,
        "package_name": rm.package_name,
        "channel": channel,
        "cpack_generator": generator,
        "kind": kind,
        "leg_id": lid,
        "job_key": job_key(rm.package_name, version.tag_suffix if version.tag else "rehearsal", lid, event.sha),
        "sha": event.sha,
        "head_sha": event.head_sha,
        "state": state,
        "exclude": list(meta.exclude),
    }
    if channel == CHANNEL_HPC_MODULE:
        # The version directory and module version: the tag as pushed, as v2 names them (``2.5.0-rc1``,
        # ``2.5.0-RC1``); the project version for a rehearsal, which installs nothing.
        hpc_version = version.tag or version.base
        prefix = hpc_prefix(
            leg["install-prefix"],
            root=check_hpc_root(hpc_root, allow_production=allow_production),
            package=rm.package_name,
            version=hpc_version,
        )
        ctx |= {
            "pkg_version": hpc_version,
            "pkg_release": "",
            "prefix": prefix,
            "filename": f"{rm.package_name}-{hpc_version}-{lid}.tar.zst",
            "nexus_target": "",
            # What hpc-sync-tag syncs, and tags (a release only; a prerelease is synced, not tagged).
            "module": {
                "name": rm.package_name,
                "version": hpc_version,
                "tag": "new" if version.mode == MODE_RELEASE else "",
            },
            "cpack_overrides": "",
            "exclude_script": "",
        }
    else:
        prefix = str(leg["install-prefix"])
        ctx |= {
            "pkg_version": version.rpm_version,
            "pkg_release": PACKAGE_RELEASE,
            "prefix": prefix,
            "filename": rpm_filename(rm.package_name, version.rpm_version, PACKAGE_RELEASE, str(leg["arch"])),
            "nexus_target": str(leg["nexus-target"]),
            "cpack_overrides": cpack_overrides(
                meta,
                generator="RPM",
                package_name=rm.package_name,
                version=version.rpm_version,
                install_prefix=prefix,
                package_release=PACKAGE_RELEASE,
            ),
            "exclude_script": exclude_script(meta.exclude, install_prefix=prefix),
        }
    ctx["prefix_sh"] = prefix_shell_word(prefix)
    filename = ctx["filename"]
    build_meta = {k: ctx[k] for k in ("mode", "tag", "version", "pkg_version", "package_name", "prefix", "channel")}
    build_meta |= {
        "repo": rm.m.repo,
        "kind": kind,
        "leg_id": lid,
        "job_key": ctx["job_key"],
        "sha": event.sha,
        "head_sha": event.head_sha,
        "filename": filename,
        "exclude": list(meta.exclude),
        "container": leg.get("container"),
        "options": leg.get("options"),
        "build_type": leg.get("build-type"),
    }
    if channel == CHANNEL_HPC_MODULE:
        build_meta |= {"site": leg.get("site"), "modules": list(leg.get("modules") or [])}
    ctx["build_meta"] = json.dumps(build_meta, indent=2, sort_keys=True)
    if BUILD_META_DELIMITER in ctx["build_meta"]:
        raise ReleaseError(f"build metadata contains the heredoc delimiter {BUILD_META_DELIMITER}")
    assert HEREDOC_DELIMITER not in ctx["build_meta"]
    return ctx


def expand(
    rm: ReleaseManifest,
    kinds: Sequence[str],
    event: ReleaseEvent,
    *,
    allow_production: bool = False,
    hpc_root: str = DEFAULT_HPC_ROOT,
) -> dict[str, list[dict[str, Any]]]:
    """Per kind, its legs with ``_release`` (state ``build``); no ``_resolved``.

    ``hpc_root``: where the hpc-module legs install (their ``{root}``).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, str] = {}
    artifacts: dict[tuple[str, str], str] = {}
    for kind in kinds:
        if not is_release_kind(kind):
            raise ReleaseError(f"kind {kind!r} is not a release kind (release-<channel>)")
        mk = rm.m.matrices.get(kind)
        if mk is None:
            raise ReleaseError(f"{rm.m.path} has no [matrix.{kind}]")
        if not mk.legs:
            raise ReleaseError(f"[matrix.{kind}] has no legs")
        legs = []
        for leg in mk.legs:
            if "_release" in leg or "_resolved" in leg:
                raise ReleaseError(f"[matrix.{kind}] a leg may not set _release or _resolved itself")
            ctx = release_context(rm, kind, leg, event, allow_production=allow_production, hpc_root=hpc_root)
            if (twin := seen.get(ctx["leg_id"])) is not None:
                raise ReleaseError(f"[matrix.{kind}] two legs share the leg id {ctx['leg_id']!r} (also in {twin})")
            seen[ctx["leg_id"]] = kind
            # Two legs writing one Nexus file or one HPC prefix: the second would be "already published"
            # with the first's bytes.
            if ctx["channel"] == CHANNEL_HPC_MODULE:
                artifact = ("hpc", ctx["prefix"])
                where = f"the HPC prefix {ctx['prefix']}"
            else:
                artifact = (ctx["nexus_target"], ctx["filename"])
                where = f"{ctx['filename']} to nexus-target {ctx['nexus_target']!r}"
            if (other := artifacts.get(artifact)) is not None:
                raise ReleaseError(f"[matrix.{kind}] legs {other!r} and {ctx['leg_id']!r} both publish {where}")
            artifacts[artifact] = ctx["leg_id"]
            legs.append({**leg, "_release": ctx})
        out[kind] = legs
    return out
