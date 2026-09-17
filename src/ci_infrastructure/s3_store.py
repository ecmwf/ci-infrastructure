#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""The artifact storage backend.

Every compiled package is stored as a single ``<artifact-name>.tar.gz`` object in
an S3-compatible bucket (the configured object store that also backs sccache),
keyed *purely by its artifact name*:

    s3://<bucket>/<key-prefix><artifact-name>.tar.gz


Library and CLI:

    python -m ci_infrastructure.s3_store upload   --name <artifact-name> --file <tar.gz>
    python -m ci_infrastructure.s3_store download --name <artifact-name> --dest <tar.gz>
    python -m ci_infrastructure.s3_store exists    --name <artifact-name>

Configuration comes from the environment; the store's location has no built-in
default, the deployment supplies it. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
are picked up by boto3 directly and are mandatory.

    ARTIFACT_S3_ENDPOINT    required: S3-compatible endpoint URL
    ARTIFACT_S3_BUCKET      required: bucket name
    ARTIFACT_S3_REGION      default: RegionOne
    ARTIFACT_S3_USE_SSL     default: true
    ARTIFACT_S3_KEY_PREFIX  default: ''   (objects live at the bucket root)
    ARTIFACT_S3_CA_BUNDLE   default: the HARICA root vendored with this package

TLS is verified against the vendored root only (see ``_ca_bundle``).
"""

from __future__ import annotations

import os
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
    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> Any: ...


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


def artifact_key(name: str) -> str:
    """``<key-prefix><name>.tar.gz``, independent of the repo or run that uploads it."""
    return f"{_key_prefix()}{name}.tar.gz"


def _client(client: _S3Client | None = None) -> _S3Client:
    """The injected client, or a boto3 client built from the env config."""
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
    """Upload a local tar.gz as the artifact's object."""
    s3 = _client(client)
    s3.upload_file(str(tar_path), _bucket(), artifact_key(name))


def download(name: str, dest_tar: str | Path, client: _S3Client | None = None) -> bool:
    """Download the artifact's object to ``dest_tar``. False if it is absent."""
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


def list_with_prefix(name_prefix: str, limit: int = 12, client: _S3Client | None = None) -> list[str]:
    """Up to ``limit`` artifact names starting with ``name_prefix-``."""
    s3 = _client(client)
    key_prefix = _key_prefix()
    resp = s3.list_objects_v2(Bucket=_bucket(), Prefix=f"{key_prefix}{name_prefix}-")
    names: list[str] = []
    for obj in resp.get("Contents", []):
        key = obj.get("Key", "")
        if key.startswith(key_prefix) and key.endswith(".tar.gz"):
            names.append(key[len(key_prefix) : -len(".tar.gz")])
            if len(names) >= limit:
                break
    return names


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


if __name__ == "__main__":
    main()
