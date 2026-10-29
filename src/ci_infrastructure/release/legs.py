# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Release legs: the manifest's `[matrix.release-*]` legs, validated, each with its ``_release`` context.

``_release`` is everything the release overlay templates read as ``release``; it is rendered
here, once, so the job script needs nothing but bash (and ``ci-infrastructure-render --release``
reproduces it offline). Release legs carry no ``_resolved``: their deps come from ``release-ref``.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from .._github_api import EXECUTION_RUNNER, ManifestSchemaError
from ..generate_downstream_ci import Manifest, SchemaError, parse_manifest
from ..manifest import ManifestFile, is_release_kind
from ..manifest import validate as validate_manifest
from . import ReleaseError
from .cpack import HEREDOC_DELIMITER, cpack_overrides, exclude_script, rpm_filename
from .detect import ReleaseEvent
from .version import PACKAGE_RELEASE

State = Literal["build", "already-published"]
STATE_BUILD: Final[State] = "build"
STATE_ALREADY_PUBLISHED: Final[State] = "already-published"

#: Channels milestone 1 builds; each maps to its CPack generator.
CHANNELS: Final[Mapping[str, str]] = {"rpm": "RPM"}

#: Release builds and their install-test images come from our registry only.
TRUSTED_REGISTRY: Final = "eccr.ecmwf.int/"
PRODUCTION_ROOT: Final = "/usr/local/apps"

BUILD_META_DELIMITER: Final = "CI_RELEASE_BUILD_META"

_ID_UNSAFE_RE: Final = re.compile(r"[^A-Za-z0-9._-]")
_PREFIX_COMPONENT_RE: Final = re.compile(r"^[A-Za-z0-9._+-]+$")
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9._-]+$")


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


def _require_str(kind: str, leg: Mapping[str, Any], key: str) -> str:
    value = leg.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"[matrix.{kind}] leg {_describe(leg)} needs {key!r} (a non-empty string)")
    return value


def _describe(leg: Mapping[str, Any]) -> str:
    return "(" + ", ".join(f"{k}={leg[k]}" for k in ("platform", "channel") if k in leg) + ")"


def validate_leg(kind: str, leg: Mapping[str, Any], *, execution: str, allow_production: bool = False) -> None:
    """The fields a release leg must set; what CI legs may leave to defaults, a release may not."""
    channel = _require_str(kind, leg, "channel")
    if channel not in CHANNELS:
        raise ReleaseError(f"[matrix.{kind}] channel {channel!r}: this release lane builds {sorted(CHANNELS)} only")
    if execution != EXECUTION_RUNNER:
        raise ReleaseError(f"[matrix.{kind}] execution {execution!r}: the {channel} channel builds on a runner")
    spec = _require_str(kind, leg, "job-script")
    if not spec.endswith(".j2"):
        raise ReleaseError(f"[matrix.{kind}] job-script {spec!r} must be a .j2 recipe extending cmake-all-lanes")
    # Options name the CMake preset, as in the base template; a release names it explicitly.
    _require_str(kind, leg, "options")
    if leg.get("tests", True) is False:
        raise ReleaseError(f"[matrix.{kind}] a release leg runs its tests: drop tests = false")
    for key in ("container", "install-test-image"):
        if not _require_str(kind, leg, key).startswith(TRUSTED_REGISTRY):
            raise ReleaseError(f"[matrix.{kind}] {key} {leg[key]!r} is not from {TRUSTED_REGISTRY}")
    _require_str(kind, leg, "install-test-command")
    _require_str(kind, leg, "build-type")
    for key in ("arch", "nexus-target", "platform"):
        if not _TOKEN_RE.fullmatch(_require_str(kind, leg, key)):
            raise ReleaseError(f"[matrix.{kind}] {key} {leg[key]!r} must match {_TOKEN_RE.pattern}")
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
) -> dict[str, Any]:
    mk = rm.m.matrices[kind]
    validate_leg(kind, leg, execution=mk.execution, allow_production=allow_production)
    meta = rm.raw.release
    assert meta is not None
    version = event.version
    channel = str(leg["channel"])
    generator = CHANNELS[channel]
    prefix = str(leg["install-prefix"])
    compiler_inputs = rm.m.packages[mk.packages[0]].compiler_inputs if mk.packages else rm.m.compiler_inputs
    lid = leg_id(kind, leg, compiler_inputs)
    filename = rpm_filename(rm.package_name, version.rpm_version, PACKAGE_RELEASE, str(leg["arch"]))
    ctx: dict[str, Any] = {
        "mode": version.mode,
        "tag": version.tag or "",
        "version": version.base,
        "pkg_version": version.rpm_version,
        "pkg_release": PACKAGE_RELEASE,
        "package_name": rm.package_name,
        "prefix": prefix,
        "channel": channel,
        "cpack_generator": generator,
        "kind": kind,
        "leg_id": lid,
        "job_key": job_key(rm.package_name, version.tag_suffix if version.tag else "rehearsal", lid, event.sha),
        "sha": event.sha,
        "head_sha": event.head_sha,
        "state": state,
        "filename": filename,
        "nexus_target": str(leg["nexus-target"]),
    }
    ctx["cpack_overrides"] = cpack_overrides(
        meta,
        generator="RPM",
        package_name=rm.package_name,
        version=version.rpm_version,
        install_prefix=prefix,
        package_release=PACKAGE_RELEASE,
    )
    ctx["exclude"] = list(meta.exclude)
    ctx["exclude_script"] = exclude_script(meta.exclude, install_prefix=prefix)
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
) -> dict[str, list[dict[str, Any]]]:
    """Per kind, its legs with ``_release`` (state ``build``); no ``_resolved``."""
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
            ctx = release_context(rm, kind, leg, event, allow_production=allow_production)
            if (twin := seen.get(ctx["leg_id"])) is not None:
                raise ReleaseError(f"[matrix.{kind}] two legs share the leg id {ctx['leg_id']!r} (also in {twin})")
            seen[ctx["leg_id"]] = kind
            # Two legs writing one Nexus file: the second would be "already published" with the first's bytes.
            artifact = (ctx["nexus_target"], ctx["filename"])
            if (other := artifacts.get(artifact)) is not None:
                raise ReleaseError(
                    f"[matrix.{kind}] legs {other!r} and {ctx['leg_id']!r} both publish {ctx['filename']} "
                    f"to nexus-target {ctx['nexus_target']!r}"
                )
            artifacts[artifact] = ctx["leg_id"]
            legs.append({**leg, "_release": ctx})
        out[kind] = legs
    return out
