#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Round-trip an object through every configured bucket, so a bad endpoint or
credential fails in the smoke test rather than in a downstream publish.

    ARTIFACT_S3_ENDPOINT   required: object store URL
    ARTIFACT_S3_BUCKET     artifact bucket, probed when set
    SCCACHE_BUCKET         sccache bucket, probed when set
    ARTIFACT_S3_REGION     default: RegionOne
    AWS_ACCESS_KEY_ID      required, read by boto3 itself
    AWS_SECRET_ACCESS_KEY

The two bucket fingerprints should differ. At least one bucket must be set.

The S3 error code is the point of the output:

    InvalidAccessKeyId     key unknown to THIS store (wrong store, or revoked)
    SignatureDoesNotMatch  key known, secret wrong or the two halves swapped
    AccessDenied           pair valid, but no rights on this bucket
    NoSuchBucket           auth fine, bucket absent from this store

A bare HTTP status ("404") instead means the reply did not come from the store;
the printed server and request id say who answered.

No secret value is printed, only truncated digests.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from typing import Any
from urllib.parse import urlsplit

import boto3
import botocore
from botocore.exceptions import ClientError

from ci_infrastructure import s3_store

_BUCKET_VARS = (("artifacts", "ARTIFACT_S3_BUCKET"), ("sccache", "SCCACHE_BUCKET"))


def _env(name: str) -> str:
    "Environment value with surrounding whitespace removed."
    return os.environ.get(name, "").strip()


def _fingerprint(value: str) -> str:
    """Short digest, to compare values across environments without printing them."""
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "<unset>"


def _describe_response(exc: ClientError) -> str:
    """Who answered, in one line: HTTP status, server, request id."""
    meta = exc.response.get("ResponseMetadata", {})
    headers = {k.lower(): v for k, v in meta.get("HTTPHeaders", {}).items()}
    fields = {
        "http": meta.get("HTTPStatusCode"),
        "server": headers.get("server"),
        "request-id": headers.get("x-amz-request-id") or meta.get("RequestId"),
        "message": exc.response.get("Error", {}).get("Message") or None,
    }
    return ", ".join(f"{k}={v}" for k, v in fields.items() if v)


def round_trip(client: Any, role: str, bucket: str, key: str) -> str | None:
    """PUT/GET/DELETE one bucket; a failure summary, or None if it passed.

    Needs its own client: a rejected ``Expect: 100-continue`` PUT leaves the pooled
    connection undrained, and a shared pool would hand its reply to the next bucket.
    """
    print(f"::group::{role} bucket")
    print(f"  bucket fingerprint: {_fingerprint(bucket)}")
    # Path vs virtual-host addressing decides which host is contacted.
    urls: list[str] = []

    def _record(request: Any, **_: Any) -> None:
        urls.append(request.url)

    client.meta.events.register_first("before-send.s3.*", _record)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"probe")
        client.get_object(Bucket=bucket, Key=key)["Body"].read()
        client.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        failure = str(exc.response["Error"].get("Code"))
        detail = _describe_response(exc)
    except Exception as exc:
        failure = type(exc).__name__
        detail = str(exc)
    else:
        failure = detail = ""
    client.meta.events.unregister("before-send.s3.*", _record)
    if urls:
        style = "path" if urlsplit(urls[0]).hostname == _endpoint_host() else "virtual-host"
        print(f"  addressing: {style}")
    print(f"  put/get/delete: {failure or 'OK'}")
    if detail:
        print(f"  responder: {detail}")
    print("::endgroup::")
    return f"{role}/{failure}" if failure else None


def _endpoint_host() -> str | None:
    return urlsplit(_env("ARTIFACT_S3_ENDPOINT")).hostname


def main() -> int:
    endpoint = _env("ARTIFACT_S3_ENDPOINT")
    if not endpoint:
        print("::error::ARTIFACT_S3_ENDPOINT is not set")
        return 1

    buckets = {role: _env(var) for role, var in _BUCKET_VARS if _env(var)}
    if not buckets:
        print("::error::set ARTIFACT_S3_BUCKET and/or SCCACHE_BUCKET -- there is nothing to probe")
        return 1

    print("::group::configuration")
    print(f"  botocore: {botocore.__version__}")
    for var in ("ARTIFACT_S3_ENDPOINT", "ARTIFACT_S3_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        print(f"  {var}: {_fingerprint(_env(var))}")
    proxies = {v: _env(v) for v in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if _env(v)}
    print(f"  proxy env: {', '.join(sorted(proxies)) if proxies else 'none'}")
    print("::endgroup::")

    # Like s3_store._client(); one per bucket (see round_trip).
    def new_client() -> Any:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=_env("ARTIFACT_S3_REGION") or "RegionOne",
            verify=str(s3_store._ca_bundle()),
        )

    key = f"_smoke/probe-{uuid.uuid4().hex}"

    failures = [f for role, bucket in buckets.items() if (f := round_trip(new_client(), role, bucket, key))]
    if failures:
        print("::error::object store probe failed: " + "; ".join(failures))
        return 1
    print(f"object store reachable and writable: {', '.join(sorted(buckets))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
