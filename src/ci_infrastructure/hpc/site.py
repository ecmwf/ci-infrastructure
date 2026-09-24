# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Typed wrapper over troika's ``get_config`` / ``get_site``, and remote path expansion."""

from __future__ import annotations

import re
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Final, Protocol, cast

from troika.config import get_config
from troika.site import get_site

from .._errors import CIError


class SlurmSiteLike(Protocol):
    """The subset of troika's ``SlurmSite`` API the orchestrator uses (incl. troika-internal names)."""

    _connection: Any

    def submit(self, script: str, user: str | None, output: str, dryrun: bool = ...) -> int: ...

    def create_output_dir(self, output: str, dryrun: bool = ...) -> Any: ...

    def _get_state(self, jid: int, strict: bool = ..., dryrun: bool = ...) -> str | None: ...

    def kill(
        self,
        script: str,
        user: str | None,
        output: str | None = ...,
        jid: int | None = ...,
        dryrun: bool = ...,
    ) -> tuple[int, str | None]: ...


def default_config_path() -> Path:
    return Path(str(resources.files("ci_infrastructure.hpc").joinpath("troika-config.yml")))


def load_site(site_name: str, *, config_path: str | Path | None = None, user: str | None = None) -> SlurmSiteLike:
    resolved = Path(config_path) if config_path is not None else default_config_path()
    config = get_config(str(resolved))
    site: Any = get_site(config, site_name, user)
    return cast(SlurmSiteLike, site)


def ensure_batch_site(site: SlurmSiteLike, site_name: str) -> None:
    """Reject non-slurm (direct) sites: the build path needs a scheduler to submit to and poll."""
    if not hasattr(site, "_get_state"):
        raise CIError(
            f"Site {site_name!r} is not a batch (slurm) site. The HPC build path needs a scheduler "
            "to submit to, reattach to and poll; use e.g. 'hpc-batch'."
        )


#: Anything else (quotes, backticks, `;`, `$(`) could run commands in the remote shell below.
_SAFE_SPEC: Final = re.compile(r"^[A-Za-z0-9_/.${}-]+$")


def resolve_remote_path(conn: Any, spec: str) -> str:
    """Expand a work-dir ``spec`` (e.g. ``$SCRATCH/github-ci``) on the cluster.

    troika quotes argv, so expand here; login shell because ``$SCRATCH`` is set in ``/etc/profile.d``.
    """
    if not _SAFE_SPEC.match(spec):
        raise CIError(
            f"Remote work dir {spec!r} contains characters that are not allowed. Use only letters, "
            "digits and _ / . - $ { } (e.g. '$SCRATCH/github-ci' or '/ec/res4/scratch/me/ci')."
        )
    proc = conn.execute(
        ["bash", "-lc", f'printf %s "{spec}"'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout, _stderr = proc.communicate()
    resolved = (stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)).strip()
    if proc.returncode != 0:
        raise CIError(f"Could not expand remote work dir {spec!r} on the cluster (exit {proc.returncode}).")
    if not resolved.startswith("/"):
        raise CIError(
            f"Remote work dir {spec!r} expanded to {resolved!r}, which is not an absolute path. "
            "An unset cluster variable expands to nothing, and a relative path is not visible to "
            "compute nodes. Check the variable exists on the cluster (e.g. $SCRATCH)."
        )
    return resolved
