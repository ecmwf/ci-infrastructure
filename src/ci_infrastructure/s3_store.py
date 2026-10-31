#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Artifact store: one ``s3://<bucket>/<key-prefix><artifact-name>.tar.gz`` object per package.

Beside it, ``<artifact-name>.deps.json`` records the dep tree the artifact was built against,
for consumers that accept an artifact built against older upstreams.

Configured from the environment only (no default location); boto3 reads
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.

    ARTIFACT_S3_ENDPOINT    required: S3-compatible endpoint URL
    ARTIFACT_S3_BUCKET      required: bucket name
    ARTIFACT_S3_REGION      default: RegionOne
    ARTIFACT_S3_USE_SSL     default: true
    ARTIFACT_S3_KEY_PREFIX  default: ''   (objects live at the bucket root)
    ARTIFACT_S3_CA_BUNDLE   default: the HARICA root vendored with this package

``gc`` deletes by write date, as for a cache: a missing artifact is rebuilt when needed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import boto3
import click
from botocore.exceptions import ClientError

from ._errors import CIError


class _S3Client(Protocol):
    def head_object(self, *, Bucket: str, Key: str) -> Any: ...
    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None: ...
    def download_file(self, Bucket: str, Key: str, Filename: str) -> None: ...
    def get_object(self, *, Bucket: str, Key: str) -> Any: ...
    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> Any: ...
    def list_objects_v2(self, *, Bucket: str, Prefix: str, ContinuationToken: str = ...) -> Any: ...
    def delete_object(self, *, Bucket: str, Key: str) -> Any: ...
    def get_bucket_versioning(self, *, Bucket: str) -> Any: ...


_DEFAULT_REGION: Final = "RegionOne"
_VENDORED_ROOT_CA: Final = Path(__file__).resolve().parent / "certs" / "harica_tls_rsa_root_ca_2021.pem"


def _require_env(var: str) -> str:
    """A required env var; no default, so boto3 never silently points at AWS."""
    value = os.environ.get(var, "")
    if not value:
        raise CIError(
            f"{var} is not set. The artifact object store must be configured via the "
            f"environment (e.g. a GitHub Actions repo/org variable); there is no default."
        )
    return value


def _bucket() -> str:
    return _require_env("ARTIFACT_S3_BUCKET")


def _key_prefix() -> str:
    return os.environ.get("ARTIFACT_S3_KEY_PREFIX", "")


def _use_ssl() -> bool:
    return os.environ.get("ARTIFACT_S3_USE_SSL", "true").strip().lower() != "false"


def _ca_bundle() -> Path:
    """ARTIFACT_S3_CA_BUNDLE, else the vendored HARICA root; never the host trust store.

    One root pins the endpoint to a single CA, and some runners' OS stores lack it.
    """
    explicit = os.environ.get("ARTIFACT_S3_CA_BUNDLE")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"ARTIFACT_S3_CA_BUNDLE points to a missing file: {path}")
        return path
    if _VENDORED_ROOT_CA.is_file():
        return _VENDORED_ROOT_CA
    raise FileNotFoundError(
        f"Vendored object-store root CA is missing at {_VENDORED_ROOT_CA} and ARTIFACT_S3_CA_BUNDLE "
        "is unset. This is a packaging error: the certs/*.pem package data was not installed. Set "
        "ARTIFACT_S3_CA_BUNDLE to a bundle that includes the object store's root CA to work around it."
    )


_TAR: Final = ".tar.gz"
_SIDECAR: Final = ".deps.json"


def artifact_key(name: str) -> str:
    return f"{_key_prefix()}{name}{_TAR}"


def sidecar_key(name: str) -> str:
    return f"{_key_prefix()}{name}{_SIDECAR}"


def _client(client: _S3Client | None = None) -> _S3Client:
    if client is not None:
        return client
    return boto3.client(
        "s3",
        endpoint_url=_require_env("ARTIFACT_S3_ENDPOINT"),
        region_name=os.environ.get("ARTIFACT_S3_REGION", _DEFAULT_REGION),
        use_ssl=_use_ssl(),
        verify=str(_ca_bundle()),
    )


def _is_not_found(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code", "") in ("404", "NoSuchKey", "NotFound")


def object_exists(name: str, client: _S3Client | None = None) -> bool:
    """True if the object is present; errors other than not-found are raised."""
    s3 = _client(client)
    try:
        s3.head_object(Bucket=_bucket(), Key=artifact_key(name))
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise
    return True


def upload(name: str, tar_path: str | Path, client: _S3Client | None = None) -> None:
    s3 = _client(client)
    s3.upload_file(str(tar_path), _bucket(), artifact_key(name))


def download(name: str, dest_tar: str | Path, client: _S3Client | None = None) -> bool:
    """False if the artifact is absent."""
    s3 = _client(client)
    dest = Path(dest_tar)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3.download_file(_bucket(), artifact_key(name), str(dest))
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise
    return True


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime


def list_objects(client: _S3Client | None = None, *, prefix: str = "") -> list[StoredObject]:
    """Every object under the key prefix plus ``prefix``, across all listing pages."""
    s3 = _client(client)
    page: dict[str, str] = {}
    out: list[StoredObject] = []
    while True:
        resp = s3.list_objects_v2(Bucket=_bucket(), Prefix=_key_prefix() + prefix, **page)
        out += [StoredObject(o["Key"], int(o["Size"]), o["LastModified"]) for o in resp.get("Contents", [])]
        if not resp.get("IsTruncated"):
            return out
        page = {"ContinuationToken": resp["NextContinuationToken"]}


@dataclass
class ArtifactIndex:
    """Which artifacts are stored, from one listing per name prefix instead of one HEAD per artifact."""

    client: _S3Client | None = None
    listings: int = 0
    _tars: dict[str, dict[str, datetime]] = field(default_factory=dict)
    _sidecars: dict[str, frozenset[str]] = field(default_factory=dict)
    _pending: dict[str, tuple[str, Mapping[str, Any]]] = field(default_factory=dict)

    def _list(self, prefix: str) -> None:
        if prefix in self._tars:
            return
        self.client = _client(self.client)
        base = len(_key_prefix())
        objects = list_objects(self.client, prefix=prefix)
        self._tars[prefix] = {o.key[base : -len(_TAR)]: o.last_modified for o in objects if o.key.endswith(_TAR)}
        self._sidecars[prefix] = frozenset(o.key[base : -len(_SIDECAR)] for o in objects if o.key.endswith(_SIDECAR))
        self.listings += 1

    def exists(self, name: str, prefix: str) -> bool:
        """``prefix`` must be a prefix of ``name``; each distinct one is listed once."""
        self._list(prefix)
        return name in self._tars[prefix]

    def with_sidecar(self, prefix: str) -> list[str]:
        """The stored artifacts under ``prefix`` that record their dep tree, newest first."""
        self._list(prefix)
        tars = self._tars[prefix]
        return sorted((n for n in tars if n in self._sidecars[prefix]), key=lambda n: tars[n], reverse=True)

    def read_sidecar(self, name: str) -> Any:
        self.client = _client(self.client)
        body = self.client.get_object(Bucket=_bucket(), Key=sidecar_key(name))["Body"].read()
        return json.loads(body)

    def record_sidecar(self, name: str, prefix: str, payload: Mapping[str, Any]) -> None:
        """Queue ``payload`` as ``name``'s sidecar; :meth:`flush_sidecars` writes the ones not yet stored."""
        self._pending.setdefault(name, (prefix, payload))

    def flush_sidecars(self) -> int:
        written = 0
        for name, (prefix, payload) in self._pending.items():
            self._list(prefix)
            if name in self._sidecars[prefix]:
                continue
            assert self.client is not None
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.client.put_object(Bucket=_bucket(), Key=sidecar_key(name), Body=body)
            written += 1
        self._pending.clear()
        return written


def eviction_plan(objects: Sequence[StoredObject], *, target: float, suffix: str = _TAR) -> list[StoredObject]:
    """The oldest-written ``suffix`` objects to delete until usage is at most ``target`` bytes."""
    used = sum(o.size for o in objects)
    plan: list[StoredObject] = []
    for obj in sorted((o for o in objects if o.key.endswith(suffix)), key=lambda o: o.last_modified):
        if used <= target:
            break
        plan.append(obj)
        used -= obj.size
    return plan


def _gb(size: float) -> str:
    return f"{size / 1e9:.1f} GB"


def stale_sidecars(
    objects: Sequence[StoredObject], evicted: Sequence[StoredObject], now: datetime
) -> list[StoredObject]:
    """The sidecars of ``evicted`` artifacts, and those over a day old whose artifact never arrived."""
    gone = {o.key.removesuffix(_TAR) for o in evicted}
    tars = {o.key.removesuffix(_TAR) for o in objects if o.key.endswith(_TAR)} - gone
    return [
        o
        for o in objects
        if o.key.endswith(_SIDECAR)
        and (stem := o.key.removesuffix(_SIDECAR)) not in tars
        and (stem in gone or now - o.last_modified >= timedelta(days=1))
    ]


def gc(
    *, percent: float, quota: int | None, dryrun: bool, suffix: str = _TAR, client: _S3Client | None = None
) -> list[StoredObject]:
    """Delete down to ``percent`` of ``quota``, or of current usage without one, and the sidecars left behind."""
    s3 = _client(client)
    versioning = s3.get_bucket_versioning(Bucket=_bucket()).get("Status", "never enabled")
    objects = list_objects(s3)
    used = sum(o.size for o in objects)
    of = f" of {_gb(quota)} ({used / quota:.0%})" if quota else ""
    print(f"s3://{_bucket()}/{_key_prefix()}: {len(objects)} objects, {_gb(used)}{of}; versioning {versioning}")
    if versioning != "never enabled":
        print("::warning::bucket versioning is on: a delete leaves the old version behind and frees nothing")
    plan = eviction_plan(objects, target=(quota if quota else used) * percent / 100, suffix=suffix)
    plan += [o for o in stale_sidecars(objects, plan, datetime.now(UTC)) if o not in plan]
    if not plan:
        print("Nothing to delete.")
        return plan
    verb = "Would delete" if dryrun else "Deleting"
    print(f"{verb} {len(plan)} oldest-written objects ({_gb(sum(o.size for o in plan))}):")
    for obj in plan:
        print(f"- {obj.key} (written {obj.last_modified:%Y-%m-%d}, {_gb(obj.size)})")
        if not dryrun:
            s3.delete_object(Bucket=_bucket(), Key=obj.key)
    return plan


@click.group(help="Publish/download/check artifacts in the S3 artifact store.")
def main() -> None:
    pass


@main.command("upload", help="Upload a local tar.gz as the named artifact.")
@click.option("--name", required=True, help="Artifact name (without the .tar.gz suffix)")
@click.option("--file", "file", required=True, help="Path to the local tar.gz to upload")
def _upload(name: str, file: str) -> None:
    upload(name, file)
    print(f"Uploaded {file} to s3://{_bucket()}/{artifact_key(name)}")


@main.command("download", help="Download the named artifact to a local path.")
@click.option("--name", required=True, help="Artifact name (without the .tar.gz suffix)")
@click.option("--dest", required=True, help="Local path to write the tar.gz to")
def _download(name: str, dest: str) -> None:
    if not download(name, dest):
        loc = f"s3://{_bucket()}/{artifact_key(name)}"
        raise CIError(f"Artifact '{name}' not found in {loc}")
    print(f"Downloaded s3://{_bucket()}/{artifact_key(name)} to {dest}")


@main.command("exists", help="Exit 0 if the named artifact exists, 1 otherwise.")
@click.option("--name", required=True, help="Artifact name (without the .tar.gz suffix)")
@click.pass_context
def _exists(ctx: click.Context, name: str) -> None:
    found = object_exists(name)
    print("true" if found else "false")
    ctx.exit(0 if found else 1)


@main.command("gc", help="Delete the oldest-written objects down to a share of the quota, or of current usage.")
@click.option("--percent", type=float, default=50, show_default=True, help="Usage to delete down to")
@click.option("--quota-gb", "quota_gb", type=float, default=None, help="The store's quota; omitted, current usage")
@click.option(
    "--suffix", default=".tar.gz", show_default=True, help="Delete only keys ending in this; '' for every object"
)
@click.option("--dryrun", is_flag=True, default=False, help="List what would be deleted; delete nothing")
def _gc(percent: float, quota_gb: float | None, suffix: str, dryrun: bool) -> None:
    gc(percent=percent, quota=int(quota_gb * 1e9) if quota_gb else None, dryrun=dryrun, suffix=suffix)


if __name__ == "__main__":
    main()
