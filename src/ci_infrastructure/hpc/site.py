# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Loading troika sites as a Python library.

Thin typed wrapper over troika's ``get_config`` / ``get_site`` so the rest of
the HPC backend talks to a small, explicit interface (`SlurmSiteLike`) instead
of the untyped troika objects. The same protocol is what the unit tests'
fake site implements, so the orchestrator can be exercised without a cluster.

Also home to :func:`resolve_remote_path`, which turns a work-dir spec that names
cluster variables (``$SCRATCH/github-ci``) into the literal path the runner
needs in order to scp into it.
"""

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
    """The subset of troika's ``SlurmSite`` API the orchestrator relies on.

    ``_get_state`` and ``_connection`` are troika-internal names; we depend on
    them deliberately because they are the cleanest programmatic poll/transport
    troika exposes (the public ``monitor`` only writes a ``.stat`` file).
    """

    #: troika connection object (untyped upstream) used to tail the job output.
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
    """Path to the troika-config.yml shipped alongside this package."""
    return Path(str(resources.files("ci_infrastructure.hpc").joinpath("troika-config.yml")))


def load_site(site_name: str, *, config_path: str | Path | None = None, user: str | None = None) -> SlurmSiteLike:
    """Build a troika site object for ``site_name`` from the given (or packaged) config."""
    resolved = Path(config_path) if config_path is not None else default_config_path()
    config = get_config(str(resolved))
    site: Any = get_site(config, site_name, user)
    return cast(SlurmSiteLike, site)


def ensure_batch_site(site: SlurmSiteLike, site_name: str) -> None:
    """Reject a site the orchestrator cannot actually drive.

    Everything here is built around a batch scheduler: ``submit`` returns a job
    id we persist for reattach, ``_get_state`` is the liveness guard, and the
    job writes its own output on the cluster for us to tail. troika's ``direct``
    sites (``hpc-login``) have none of that — ``submit`` hands back a Popen,
    there is no ``_get_state``, and troika opens the output *locally*. Without
    this check that mismatch surfaces as a TypeError deep inside submit.
    """
    if not hasattr(site, "_get_state"):
        raise CIError(
            f"Site {site_name!r} is not a batch (slurm) site. The HPC build path needs a scheduler "
            "to submit to, reattach to and poll; use e.g. 'hpc-batch'."
        )


#: A work-dir spec may only name variables and path characters. Anything else
#: (quotes, backticks, `;`, spaces, `$(`) would let the spec run commands on the
#: cluster once we hand it to a remote shell below.
_SAFE_SPEC: Final = re.compile(r"^[A-Za-z0-9_/.${}-]+$")


def resolve_remote_path(conn: Any, spec: str) -> str:
    """Expand a work-dir ``spec`` on the cluster and return the literal path.

    A spec like ``$SCRATCH/github-ci`` cannot be used as-is: troika
    ``shlex.quote``s every argv element, so cluster variables passed to
    ``mkdir``/``scp`` would arrive literally and we would create a directory
    actually named ``$SCRATCH``. Expanding it here, once, gives the runner a real
    path while keeping the configured value portable across clusters (and free of
    the deploy username).

    The expansion runs in a **login** shell: on ECMWF's atos, ``$SCRATCH`` is set
    by ``ecprofile`` under ``/etc/profile.d``, which a plain non-interactive ssh
    shell does not source. ``printf %s`` keeps stdout free of any profile banner
    a login shell might print (that lands on stderr, which we discard).
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
