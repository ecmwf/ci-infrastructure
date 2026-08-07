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

No secret value is printed: this repository is public and the endpoint is
itself held as a secret.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

from ci_infrastructure import s3_store

_BUCKET_VARS = (("artifacts", "ARTIFACT_S3_BUCKET"), ("sccache", "SCCACHE_BUCKET"))


def _env(name: str) -> str:
    "Environment value with surrounding whitespace removed."
    return os.environ.get(name, "").strip()


def round_trip(client: Any, role: str, bucket: str, key: str) -> str | None:
    """PUT/GET/DELETE one bucket. Return a failure summary, or None if it passed.

    Write, read back and clean up -- exactly what publish and fetch do, so a
    pass here means those will work.
    """
    print(f"::group::{role} bucket")
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"probe")
        client.get_object(Bucket=bucket, Key=key)["Body"].read()
        client.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        failure = str(exc.response["Error"].get("Code"))
    except Exception as exc:
        failure = type(exc).__name__
    else:
        failure = ""
    print(f"  put/get/delete: {failure or 'OK'}")
    print("::endgroup::")
    return f"{role}/{failure}" if failure else None


def main() -> int:
    endpoint = _env("ARTIFACT_S3_ENDPOINT")
    if not endpoint:
        print("::error::ARTIFACT_S3_ENDPOINT is not set")
        return 1

    buckets = {role: _env(var) for role, var in _BUCKET_VARS if _env(var)}
    if not buckets:
        print("::error::set ARTIFACT_S3_BUCKET and/or SCCACHE_BUCKET -- there is nothing to probe")
        return 1

    # Built like s3_store._client(), vendored HARICA root included, so a pass
    # here means the real publish path will work.
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=_env("ARTIFACT_S3_REGION") or "RegionOne",
        verify=str(s3_store._ca_bundle()),
    )
    key = f"_smoke/probe-{uuid.uuid4().hex}"

    failures = [f for role, bucket in buckets.items() if (f := round_trip(client, role, bucket, key))]
    if failures:
        print("::error::object store probe failed: " + "; ".join(failures))
        return 1
    print(f"object store reachable and writable: {', '.join(sorted(buckets))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
