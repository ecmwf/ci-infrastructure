#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
s3_store.py

The artifact storage backend. Every compiled package is stored as a single
``<artifact-name>.tar.gz`` object in an S3-compatible bucket (the configured
object store that also backs sccache), keyed *purely by its artifact name*:

    s3://<bucket>/<key-prefix><artifact-name>.tar.gz

The artifact name already encodes the full identity of a build
(``<prefix>-<sha>[-<deps-hash8>]-<platform>-<compiler>[-py<ver>]-<build-type>[-opts.<option>]``)
and is independent of which repository or workflow run produced it. The trailing
``opts.`` segment is present only for builds that name a build-option config
(a feature configuration orthogonal to build-type); plain builds omit it. Keying the
object store by that name — rather than by the producer repo's GitHub Actions
artifact store — is what lets any consumer resolve a dependency regardless of
which run built it. That decoupling is the whole reason this module exists.

This module is both a library (imported by resolve_deps / check_artifact /
fetch_deps for existence checks and downloads) and a CLI (invoked from the
composite actions for publish/download):

    python -m ci_infrastructure.s3_store upload   --name <artifact-name> --file <tar.gz>
    python -m ci_infrastructure.s3_store download --name <artifact-name> --dest <tar.gz>
    python -m ci_infrastructure.s3_store exists    --name <artifact-name>

Configuration is read from the environment. The object store's location has no
built-in default — the deployment supplies it (e.g. via GitHub Actions repo/org
variables). The endpoint and bucket are required; the AWS credentials
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, picked up by boto3 directly) are
mandatory too:

    ARTIFACT_S3_ENDPOINT    required: S3-compatible endpoint URL
    ARTIFACT_S3_BUCKET      required: bucket name
    ARTIFACT_S3_REGION      default: RegionOne
    ARTIFACT_S3_USE_SSL     default: true
    ARTIFACT_S3_KEY_PREFIX  default: ''   (objects live at the bucket root)
    ARTIFACT_S3_CA_BUNDLE   default: the HARICA root vendored with this package

TLS trust is deliberately narrow: this client only ever talks to the configured
object store, whose certificate chain is anchored by a single root (HARICA TLS
RSA Root CA 2021). Rather than trust the runner's whole OS trust store — which
on some runners (notably the HPC login nodes) is too old to include that root —
we verify against just the vendored root. That both fixes the stale-store
failure and pins the object store to one CA, so a mis-issuance by any other
public CA cannot be used to intercept it. Point ARTIFACT_S3_CA_BUNDLE at your
own bundle to override (e.g. if the store is ever re-hosted behind another CA).
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
# The single root that anchors the object-store certificate chain, vendored
# as package data (see certs/harica_tls_rsa_root_ca_2021.pem). Both botocore's
# bundled cacert.pem and some runners' OS trust stores lag this root, so we ship
# it ourselves rather than depend on whatever the host happens to trust.
_VENDORED_ROOT_CA: Final = Path(__file__).resolve().parent / "certs" / "harica_tls_rsa_root_ca_2021.pem"


def _require_env(var: str) -> str:
    """Return a required env var's value, or raise a clear CIError if unset/empty.

    The object store's location has no baked-in default: the deployment supplies
    it (e.g. via GitHub Actions repo/org variables). Failing loudly here beats
    silently pointing boto3 at real AWS S3.
    """
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
    # Mirror the sccache action's string flag; anything but an explicit
    # "false" (case-insensitive) keeps SSL on.
    return os.environ.get("ARTIFACT_S3_USE_SSL", "true").strip().lower() != "false"


def _ca_bundle() -> Path:
    """Path to the CA bundle used to verify the S3 endpoint's certificate.

    We never fall back to the host trust store and never disable verification:
    an explicit ARTIFACT_S3_CA_BUNDLE wins, otherwise we verify against just the
    vendored object-store root. This keeps the trust anchor set minimal (the one
    CA that actually signs the endpoint) and makes verification independent of
    however stale the runner's OS bundle is. If neither is a real file we fail
    loudly rather than silently trusting something wider.
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
    """S3 object key for an artifact name: ``<key-prefix><name>.tar.gz``.

    The key is derived solely from the artifact name, so the same build maps to
    the same object no matter which repo or run uploads it.
    """
    return f"{_key_prefix()}{name}.tar.gz"


def _client(client: _S3Client | None = None) -> _S3Client:
    """Return the injected client, or build one from the env config.

    Tests pass an in-memory fake; production code lets this construct a real
    boto3 S3 client pointed at the object store's custom endpoint.
    """
    if client is not None:
        return client
    return boto3.client(
        "s3",
        endpoint_url=_require_env("ARTIFACT_S3_ENDPOINT"),
        region_name=os.environ.get("ARTIFACT_S3_REGION", _DEFAULT_REGION),
        use_ssl=_use_ssl(),
        verify=str(_ca_bundle()),
    )


def object_exists(name: str, client: _S3Client | None = None) -> bool:
    """True if the artifact's object is present in the bucket.

    A 404 / NoSuchKey / NotFound is the expected "absent" answer and returns
    False; any other ClientError is re-raised so genuine auth/endpoint problems
    are not silently read as "missing artifact".
    """
    s3 = _client(client)
    try:
        s3.head_object(Bucket=_bucket(), Key=artifact_key(name))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
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
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def list_with_prefix(name_prefix: str, limit: int = 12, client: _S3Client | None = None) -> list[str]:
    """Return up to ``limit`` artifact names whose name starts with ``name_prefix-``.

    Used for the missing-artifact diagnostics; strips the key-prefix and the
    ``.tar.gz`` suffix so callers get artifact names back, not raw keys.
    """
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
    # The exit code is this command's output: a boolean predicate, like `grep -q`.
    # Not error handling — a missing artifact is a legitimate "false", not a failure.
    found = object_exists(name)
    print("true" if found else "false")
    ctx.exit(0 if found else 1)


if __name__ == "__main__":
    main()
