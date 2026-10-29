# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Publish one package file to a Nexus raw/yum repository, at most once.

A version is immutable: the same bytes already there is a no-op (a re-run), different bytes
fail. Only the bytes the install test passed are uploaded (their digest is in its receipt).
Test Nexus only, unless allow-production. The credential comes from the environment only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import click

from . import ReleaseError

TEST_HOSTS: Final = frozenset({"nexus-test.ecmwf.int"})
PRODUCTION_HOSTS: Final = frozenset({"nexus.ecmwf.int"})

#: ``user:token``, as curl --user takes it.
AUTH_ENV: Final = "NEXUS_AUTH"

_RETRIES: Final = 3
_FILENAME_SAFE: Final = "~._+-"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""


class Http(Protocol):
    def __call__(self, method: str, url: str, *, data: bytes | None = None, auth: str | None = None) -> Response: ...


def urllib_http(method: str, url: str, *, data: bytes | None = None, auth: str | None = None) -> Response:
    request = urllib.request.Request(url, data=data, method=method)
    if auth:
        request.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    if data is not None:
        request.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(request, timeout=300) as resp:  # noqa: S310 - https only, see check_url
            return Response(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # 0: no HTTP status at all; the caller retries or reports Nexus unreachable.
        return Response(0, str(exc).encode())


def check_url(url: str, *, allow_production: bool = False, allowed_hosts: frozenset[str] | None = None) -> str:
    """An https URL on a test Nexus host (or a production one with allow-production); no credentials in it.

    The URL is the repository v2 PUTs ``<url>/<file>`` to, kept in an org secret
    (NEXUS_TEST_REPO_URL_*): GitHub masks it in logs, so errors name its parts, not the URL.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    hosts = (
        allowed_hosts if allowed_hosts is not None else TEST_HOSTS | (PRODUCTION_HOSTS if allow_production else set())
    )
    if allowed_hosts is None and parsed.scheme != "https":
        raise ReleaseError(f"Nexus URL must be https, not {parsed.scheme or '(no scheme)'!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReleaseError("Nexus URL must not carry credentials, a query or a fragment")
    try:
        port = parsed.port
    except ValueError:
        port = -1
    # The host, any letter case, on the default https port, written or not.
    host = parsed.hostname or ""
    if host not in hosts or port not in (None, 443):
        what = "a test Nexus host" if not allow_production else "a Nexus host"
        raise ReleaseError(f"Nexus URL host {parsed.netloc!r} is not {what} ({sorted(hosts)})")
    return url.strip().rstrip("/")


def file_url(base_url: str, filename: str) -> str:
    if not filename or "/" in filename or filename in (".", ".."):
        raise ReleaseError(f"not a plain file name: {filename!r}")
    return f"{base_url}/{urllib.parse.quote(filename, safe=_FILENAME_SAFE)}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _with_retries(call: Callable[[], Response], what: str, sleep: Callable[[float], None]) -> Response:
    resp = call()
    for attempt in range(1, _RETRIES):
        if resp.status and resp.status < 500:
            return resp
        print(f"::warning::{what}: HTTP {resp.status or 'no response'}; retrying ({attempt}/{_RETRIES - 1})")
        sleep(5 * attempt)
        resp = call()
    return resp


def probe(base_url: str, filename: str, *, http: Http = urllib_http) -> Literal["present", "absent", "unknown"]:
    """Anonymous: is the file there? ``unknown`` when Nexus does not say (unreachable, auth, 5xx)."""
    resp = http("HEAD", file_url(base_url, filename))
    if resp.status == 200:
        return "present"
    if resp.status == 404:
        return "absent"
    print(f"::warning::Nexus probe of {filename}: HTTP {resp.status or 'no response'}; building it anyway")
    return "unknown"


Action = Literal["uploaded", "already-present", "already-published"]


def _receipt(url: str, digest: str, action: Action, filename: str) -> dict[str, Any]:
    return {
        "url": url,
        "filename": filename,
        "sha256": digest,
        "action": action,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def upload(
    path: Path,
    *,
    base_url: str,
    expected_filename: str,
    tested_sha256: str,
    auth: str,
    http: Http = urllib_http,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if path.name != expected_filename:
        raise ReleaseError(f"file {path.name!r} is not the expected {expected_filename!r}")
    digest = sha256_file(path)
    if digest != tested_sha256:
        raise ReleaseError(f"{path.name}: sha256 {digest} is not what the install test passed ({tested_sha256})")
    url = file_url(base_url, expected_filename)
    existing = _with_retries(lambda: http("GET", url, auth=auth), f"GET {url}", sleep)
    if existing.status == 200:
        there = hashlib.sha256(existing.body).hexdigest()
        if there == digest:
            print(f"{url} already holds these bytes ({digest}); nothing to do")
            return _receipt(url, digest, "already-present", expected_filename)
        raise ReleaseError(f"{url} holds other bytes (sha256 {there}, ours {digest}): versions are immutable; bump rc")
    if existing.status != 404:
        raise ReleaseError(f"GET {url}: HTTP {existing.status or 'no response'}")
    put = _with_retries(lambda: http("PUT", url, data=path.read_bytes(), auth=auth), f"PUT {url}", sleep)
    if put.status not in (200, 201, 204):
        raise ReleaseError(f"PUT {url}: HTTP {put.status or 'no response'}")
    check = _with_retries(lambda: http("GET", url, auth=auth), f"GET {url}", sleep)
    if check.status != 200 or hashlib.sha256(check.body).hexdigest() != digest:
        raise ReleaseError(f"{url}: after the upload Nexus serves other bytes (HTTP {check.status})")
    print(f"uploaded {url} ({digest})")
    return _receipt(url, digest, "uploaded", expected_filename)


def already_published(
    *, base_url: str, filename: str, auth: str, http: Http = urllib_http, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    """The receipt for a leg the gate found published: what Nexus serves, as it is now."""
    url = file_url(base_url, filename)
    resp = _with_retries(lambda: http("GET", url, auth=auth), f"GET {url}", sleep)
    if resp.status != 200:
        raise ReleaseError(f"the gate found {url} published, but now: HTTP {resp.status or 'no response'}; re-run all")
    digest = hashlib.sha256(resp.body).hexdigest()
    print(f"{url} was already published ({digest}); not rebuilt")
    return _receipt(url, digest, "already-published", filename)


def parse_urls(text: str, *, what: str) -> dict[str, Any]:
    """The JSON object of target -> URL a workflow builds around a secret (`'{"t": "${{ secrets.X }}"}'`).

    Lenient on control characters: a secret saved with a trailing newline still parses (check_url strips it).
    """
    try:
        urls = json.loads(text or "{}", strict=False)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{what} is not valid JSON ({exc.msg}); a URL must not hold a quote or backslash") from None
    if not isinstance(urls, dict):
        raise ReleaseError(f"{what} must be a JSON object")
    return urls


def target_url(urls: Mapping[str, Any], target: str, *, allow_production: bool) -> str:
    url = urls.get(target)
    if not isinstance(url, str) or not url.strip():
        raise ReleaseError(f"no Nexus URL for target {target!r} (have {sorted(urls)})")
    return check_url(url, allow_production=allow_production)


@click.command("upload-nexus")
@click.option("--urls", required=True, help="JSON object: nexus-target -> repository URL.")
@click.option("--target", required=True, help="The leg's nexus-target.")
@click.option("--filename", required=True, help="The expected package file name.")
@click.option("--leg-id", required=True)
@click.option("--state", type=click.Choice(["build", "already-published"]), default="build", show_default=True)
@click.option("--package-dir", type=click.Path(path_type=Path), help="Holds the package (state build).")
@click.option("--itest", type=click.Path(path_type=Path), help="The install test's receipt (state build).")
@click.option("--sha", default="", help="The commit released (_release.sha); the install test must be of it.")
@click.option("--job-key", default="", help="The leg's _release.job_key; the install test must be of it.")
@click.option("--receipt-dir", type=click.Path(path_type=Path), required=True)
@click.option("--allow-production", is_flag=True)
def main(
    urls: str,
    target: str,
    filename: str,
    leg_id: str,
    state: str,
    package_dir: Path | None,
    itest: Path | None,
    sha: str,
    job_key: str,
    receipt_dir: Path,
    allow_production: bool,
) -> None:
    """Upload the leg's package to Nexus (credential: $NEXUS_AUTH, user:token) and write receipt-<leg>.json."""
    auth = os.environ.get(AUTH_ENV, "")
    if not auth:
        raise ReleaseError(f"${AUTH_ENV} is empty: set the Nexus credential (user:token) in the step's env")
    base = target_url(parse_urls(urls, what="--urls"), target, allow_production=allow_production)
    if state == "already-published":
        receipt = already_published(base_url=base, filename=filename, auth=auth)
    else:
        if package_dir is None or itest is None or not sha or not job_key:
            raise click.UsageError("state build needs --package-dir, --itest, --sha and --job-key")
        tested = json.loads(itest.read_text())
        if tested.get("filename") != filename or tested.get("result") != "passed":
            raise ReleaseError(f"{itest} is not a passed install test of {filename}")
        if (tested.get("sha"), tested.get("job_key")) != (sha, job_key):
            raise ReleaseError(
                f"{itest} tested the build of commit {tested.get('sha')!r}, job {tested.get('job_key')!r}; "
                f"this release is commit {sha!r}, job {job_key!r}"
            )
        receipt = upload(
            package_dir / filename,
            base_url=base,
            expected_filename=filename,
            tested_sha256=str(tested.get("sha256")),
            auth=auth,
        )
    receipt["leg_id"] = leg_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / f"receipt-{leg_id}.json").write_text(json.dumps(receipt, indent=2) + "\n")
