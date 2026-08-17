#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""
probe_object_store.py

Round-trip an object through every configured bucket, so a bad endpoint or a
credential issued against the wrong store fails in the smoke test rather than
halfway through a downstream publish.

Configuration comes from the environment under the same names s3_store reads,
so whatever works here is what belongs in the secrets:

    ARTIFACT_S3_ENDPOINT   required: object store URL
    ARTIFACT_S3_BUCKET     artifact bucket, probed when set
    SCCACHE_BUCKET         sccache bucket, probed when set
    ARTIFACT_S3_REGION     default: RegionOne
    AWS_ACCESS_KEY_ID      required, read by boto3 itself
    AWS_SECRET_ACCESS_KEY

At least one bucket must be set. Exits non-zero if any of them fails.

The S3 error code is the whole point of the output, because the failures look
identical from the outside:

    InvalidAccessKeyId     key unknown to THIS store (wrong store, or revoked)
    SignatureDoesNotMatch  key known, secret wrong or the two halves swapped
    AccessDenied           pair valid, but no rights on this bucket
    NoSuchBucket           auth fine, bucket absent from this store

A bare HTTP status ("404") in place of one of those codes means the reply
carried no S3 error document, so it did not come from the object store and the
status says nothing about the bucket named next to it. Each failure therefore
also prints the HTTP status, the responding server and the request id: those
say *who* answered, which the error code alone cannot. A missing server header
is the tell.

No secret value is printed: this repository is public and the endpoint is
itself held as a secret. Configuration is identified only by a truncated
digest, which is enough to tell "the value CI used" from "the value I used by
hand" without disclosing either.
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
    """Short digest of a value, for comparing environments without printing it.

    Same value in two places -> same digest, so a run in CI and a run by hand
    can be told apart from a run against a different endpoint, bucket or key,
    which is the difference these probes most often turn on.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "<unset>"


def _describe_response(exc: ClientError) -> str:
    """Who answered, in one line: HTTP status, server, request id.

    The S3 error code identifies a *store* rejection. When the reply never came
    from the store -- a proxy or ingress answering instead -- there is no code
    to read, and only these fields distinguish the two.
    """
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
    """PUT/GET/DELETE one bucket. Return a failure summary, or None if it passed.

    Write, read back and clean up -- exactly what publish and fetch do, so a
    pass here means those will work.

    Takes its own client, and that is not incidental. A PUT is sent with an
    ``Expect: 100-continue`` header, so a rejection arrives before the body
    does and leaves the pooled HTTPS connection undrained. A later PUT reusing
    that connection reads the *previous* reply: the second bucket then reports
    the first bucket's status with no body to parse, which surfaces as a bare
    ``404`` rather than an S3 error code. One client per bucket means one
    connection pool per bucket, so every result is that bucket's own.
    """
    print(f"::group::{role} bucket")
    print(f"  bucket fingerprint: {_fingerprint(bucket)}")
    # The request URL is built from the endpoint and the bucket; whether the
    # bucket lands in the path or in the hostname decides which host is
    # contacted at all, so record it rather than assume path style.
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
        # Host only, and only whether it is the endpoint's: the endpoint and the
        # bucket name are both secrets, so neither is printed.
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

    # Everything that decides where the requests actually go, identified but
    # not disclosed. A probe that passes by hand and fails here is nearly
    # always a different value behind one of these names -- an org-level secret
    # shadowed in one place and not the other, a key from another store -- and
    # comparing digests settles that in one look. The proxy variables are here
    # because a proxy answering on the store's behalf is the other way a
    # request ends up somewhere unintended.
    print("::group::configuration")
    print(f"  botocore: {botocore.__version__}")
    for var in ("ARTIFACT_S3_ENDPOINT", "ARTIFACT_S3_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        print(f"  {var}: {_fingerprint(_env(var))}")
    proxies = {v: _env(v) for v in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY") if _env(v)}
    print(f"  proxy env: {', '.join(sorted(proxies)) if proxies else 'none'}")
    print("::endgroup::")

    # Built like s3_store._client(), vendored HARICA root included, so a pass
    # here means the real publish path will work. One per bucket, never shared
    # -- see round_trip on why a shared pool makes the second bucket lie.
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
