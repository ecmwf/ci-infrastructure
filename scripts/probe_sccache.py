#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Compile a small C++ project twice and prove the second compile was served from
the object store. Runs by hand exactly as in CI:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
    ARTIFACT_S3_ENDPOINT='https://...' \\
    SCCACHE_BUCKET='gh-runners-sccache' \\
    python scripts/probe_sccache.py

Configuration, in the same names the smoke test already passes:

    SCCACHE_BUCKET         required: the sccache bucket
    ARTIFACT_S3_ENDPOINT   required: object store URL (or SCCACHE_ENDPOINT)
    ARTIFACT_S3_REGION     default: RegionOne (or SCCACHE_REGION)
    ARTIFACT_S3_USE_SSL    default: inferred from the endpoint scheme
    AWS_ACCESS_KEY_ID      required, read by sccache and boto3 themselves
    AWS_SECRET_ACCESS_KEY
    CXX                    default: first of clang++-18, clang++, g++

--use-ambient-config takes SCCACHE_* as actions/setup-sccache left them (CI).

sccache silently falls back to a local disk cache when S3 is unreachable, so
"compile twice, expect a hit" proves nothing. Three checks instead:

    1. backend identity  sccache must report an S3 backend, not local disk
    2. write proof       objects must appear in THAT bucket, per boto3
    3. read proof        with the local cache erased, a hit can only be remote

Objects go under a per-run key prefix, deleted afterwards unless --keep.
No secret value is printed, only truncated digests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import boto3

from ci_infrastructure import s3_store

_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples" / "sccache"

# Tried in order when CXX is unset.
_COMPILER_CANDIDATES = ("clang++-18", "clang++", "g++")

# Our own port, so a hand run leaves the developer's sccache server alone.
_DEFAULT_SERVER_PORT = "4237"


def _env(name: str) -> str:
    "Environment value with surrounding whitespace removed."
    return os.environ.get(name, "").strip()


def _fingerprint(value: str) -> str:
    """Short digest, to compare values across environments without printing them."""
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "<unset>"


def _fail(message: str) -> None:

    print(f"::error::{message}")


def _run(cmd: list[str], env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run quietly; on failure print the captured output in a group and raise."""
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        print(f"::group::failed: {' '.join(cmd[:2])}")
        print(proc.stdout)
        print(proc.stderr)
        print("::endgroup::")
        raise RuntimeError(f"command failed with exit code {proc.returncode}: {cmd[0]}")
    return proc


def _which_or_fail(tool: str) -> str:

    found = shutil.which(tool)
    if not found:
        raise RuntimeError(f"{tool} is not on PATH -- install it, or run this where it is available")
    return found


def _pick_compiler() -> str:
    "CXX if set, else the first candidate present."
    explicit = _env("CXX")
    if explicit:
        return _which_or_fail(explicit)
    for candidate in _COMPILER_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError(f"no C++ compiler found -- set CXX, or install one of: {', '.join(_COMPILER_CANDIDATES)}")


def _counter(stats: dict[str, Any], key: str) -> int:
    """One sccache counter: a plain int, or summed from ``{"counts": {...}}``."""
    value = stats.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        counts = value.get("counts", value)
        if isinstance(counts, dict):
            return sum(v for v in counts.values() if isinstance(v, int))
    return 0


def _error_total(stats: dict[str, Any]) -> int:
    "Every error-ish counter summed: a backend that errors must not read as OK."
    return sum(_counter(stats, key) for key in stats if "error" in key.lower())


def _show_stats(sccache: str, env: dict[str, str]) -> dict[str, Any]:
    """The whole stats document: callers need both "cache_location" and "stats"."""
    proc = _run([sccache, "--show-stats", "--stats-format=json"], env)
    payload = json.loads(proc.stdout)
    return payload if isinstance(payload, dict) else {}


def _counters(payload: dict[str, Any]) -> dict[str, Any]:
    "The counter mapping, which some versions nest under 'stats' and some do not."
    stats = payload.get("stats", payload)
    return stats if isinstance(stats, dict) else {}


_REMOTE_SCHEMES = frozenset({"s3", "gcs", "azblob", "ghac", "redis", "webdav", "oss", "memcached", "cos"})


def _backend_of(payload: dict[str, Any]) -> str:
    """The backend scheme only; the location string names the (secret) bucket.

    A read-only remote reports its scheme too, hence the cache_writes check.
    """
    location = payload.get("cache_location")
    if not isinstance(location, str) or not location:
        return ""
    if location.startswith("Local disk"):
        return "local-disk"
    if location.startswith("Multi-level"):
        return "multi-level"
    head = location.split(",", 1)[0].strip()
    return head if head in _REMOTE_SCHEMES else "unrecognised"


def _redacted(payload: dict[str, Any]) -> dict[str, Any]:
    "The stats document minus cache_location, which spells out the bucket name."
    return {k: v for k, v in payload.items() if k != "cache_location"}


def _s3_client() -> Any:
    """A boto3 client built like s3_store's, vendored CA included."""
    return boto3.client(
        "s3",
        endpoint_url=_env("SCCACHE_ENDPOINT") or _env("ARTIFACT_S3_ENDPOINT"),
        region_name=_env("ARTIFACT_S3_REGION") or "RegionOne",
        verify=str(s3_store._ca_bundle()),
    )


# Written by sccache's startup write-probe, not by a compile.
_STARTUP_PROBE_KEY = ".sccache_check"


def _list_keys(client: Any, bucket: str, prefix: str) -> list[str]:

    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            return keys
        token = resp.get("NextContinuationToken")


def _delete_prefix(client: Any, bucket: str, prefix: str) -> int:

    keys = _list_keys(client, bucket, prefix)
    for start in range(0, len(keys), 1000):
        batch = keys[start : start + 1000]
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})
    return len(keys)


def _sccache_env(base: dict[str, str], key_prefix: str, cache_dir: Path, ambient: bool) -> dict[str, str]:
    """The sccache server environment; outside ambient mode derived from the artifact-store names."""
    env = dict(base)
    if not ambient:
        endpoint = _env("SCCACHE_ENDPOINT") or _env("ARTIFACT_S3_ENDPOINT")
        env["SCCACHE_ENDPOINT"] = endpoint
        # Required by sccache; it does NOT read AWS_REGION for this.
        env["SCCACHE_REGION"] = _env("SCCACHE_REGION") or _env("ARTIFACT_S3_REGION") or "RegionOne"
        env["SCCACHE_S3_USE_SSL"] = _env("ARTIFACT_S3_USE_SSL") or ("true" if endpoint.startswith("https") else "false")

    # Always ours: a fresh prefix, a disposable cache dir, our own server.
    env["SCCACHE_S3_KEY_PREFIX"] = key_prefix
    env["SCCACHE_DIR"] = str(cache_dir)
    env.setdefault("SCCACHE_SERVER_PORT", _DEFAULT_SERVER_PORT)
    # Virtual-host addressing must stay off: the store has no wildcard DNS, and
    # its certificate is a single-label wildcard that would not match anyway.
    env.pop("SCCACHE_S3_ENABLE_VIRTUAL_HOST_STYLE", None)
    # A multi-level chain puts a disk tier in front of S3 and voids the read proof.
    env.pop("SCCACHE_MULTILEVEL_CHAIN", None)
    return env


def _restart_server(sccache: str, env: dict[str, str]) -> None:
    """Restart the server: it reads its configuration only at startup."""
    _run([sccache, "--stop-server"], env, check=False)
    _run([sccache, "--start-server"], env)


def _build(cmake: str, sccache: str, cxx: str, build_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    """Configure, zero the stats (so CMake's compiler probes do not count), build; returns the stats."""
    _run(
        [
            cmake,
            "-S",
            str(_SAMPLE_DIR),
            "-B",
            str(build_dir),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_CXX_COMPILER={cxx}",
            f"-DCMAKE_CXX_COMPILER_LAUNCHER={sccache}",
        ],
        env,
    )
    _run([sccache, "--zero-stats"], env)
    _run([cmake, "--build", str(build_dir)], env)
    return _show_stats(sccache, env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--use-ambient-config",
        action="store_true",
        help="take SCCACHE_* from the environment as-is (how CI runs it, after setup-sccache)",
    )
    parser.add_argument("--keep", action="store_true", help="do not delete this run's cache objects (for debugging)")
    args = parser.parse_args()

    bucket = _env("SCCACHE_BUCKET")
    endpoint = _env("SCCACHE_ENDPOINT") or _env("ARTIFACT_S3_ENDPOINT")
    if not bucket:
        _fail("SCCACHE_BUCKET is not set -- there is no cache to probe")
        return 1
    if not endpoint:
        _fail("neither SCCACHE_ENDPOINT nor ARTIFACT_S3_ENDPOINT is set")
        return 1

    try:
        sccache = _which_or_fail("sccache")
        cmake = _which_or_fail("cmake")
        cxx = _pick_compiler()
        # Before anything starts, so a bad CA bundle or endpoint is a message, not a cleanup traceback.
        client = _s3_client()
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")
        return 1

    # Unique per run; in ambient mode nested under the action's prefix.
    ambient_prefix = _env("SCCACHE_S3_KEY_PREFIX") if args.use_ambient_config else ""
    key_prefix = f"{ambient_prefix or 'sccache-smoke'}/probe-{uuid.uuid4().hex}/"

    print("::group::configuration")
    print(f"  mode: {'ambient (setup-sccache)' if args.use_ambient_config else 'derived'}")
    print(f"  SCCACHE_BUCKET: {_fingerprint(bucket)}")
    print(f"  endpoint: {_fingerprint(endpoint)}")
    print(f"  compiler: {Path(cxx).name}")
    print(f"  sccache: {_run([sccache, '--version'], dict(os.environ), check=False).stdout.strip()}")
    print("::endgroup::")

    failures: list[str] = []

    def fail(tag: str, message: str) -> None:
        failures.append(tag)
        _fail(message)

    tmp = Path(tempfile.mkdtemp(prefix="sccache-probe-"))
    build_dir = tmp / "build"
    cache_dir = tmp / "local-cache"
    env = _sccache_env(dict(os.environ), key_prefix, cache_dir, args.use_ambient_config)

    try:
        _restart_server(sccache, env)

        # 1. Backend identity, chosen once at daemon start.
        payload = _show_stats(sccache, env)
        print("::group::sccache backend")
        # Dumped (bucket redacted) as evidence when the stats schema moves.
        print(json.dumps(_redacted(payload), indent=2, sort_keys=True))
        print("::endgroup::")
        backend = _backend_of(payload)
        print(f"  backend: {backend or '<not reported>'}")
        if backend == "local-disk":
            fail(
                "backend/local-disk",
                "sccache is using a LOCAL DISK cache, not the object store: the daemon was started before "
                "SCCACHE_BUCKET was in its environment, so every later check would pass against a dead bucket",
            )
        elif backend and backend != "s3":
            fail(f"backend/{backend}", f"sccache reports a {backend!r} backend, not s3")

        # 2. Write proof, seen by a different client.
        first = _counters(_build(cmake, sccache, cxx, build_dir, env))
        print("::group::first build (expected: a miss, and a write)")
        print(f"  requests: {_counter(first, 'compile_requests')}")
        print(f"  misses: {_counter(first, 'cache_misses')}  hits: {_counter(first, 'cache_hits')}")
        print(f"  writes: {_counter(first, 'cache_writes')}  errors: {_error_total(first)}")
        print("::endgroup::")
        if _error_total(first):
            fail("first-build/cache-errors", "sccache reported cache errors on the first build")
        if not _counter(first, "cache_misses"):
            fail("first-build/no-miss", "the first build was not a cache miss -- the key prefix was not fresh")
        if not _counter(first, "cache_writes"):
            fail("first-build/no-write", "sccache cached nothing -- the bucket accepted reads but not writes")

        written = [k for k in _list_keys(client, bucket, key_prefix) if not k.endswith(_STARTUP_PROBE_KEY)]
        print("::group::objects written to the bucket")
        print(f"  cached objects under this run's prefix: {len(written)}")
        print("::endgroup::")
        if not written:
            fail("write/no-objects", "sccache wrote nothing to the bucket -- the compile was cached somewhere else")

        # 3. Read proof: with every local trace erased, a hit came from the object store.
        _run([sccache, "--stop-server"], env, check=False)
        shutil.rmtree(cache_dir, ignore_errors=True)
        shutil.rmtree(build_dir, ignore_errors=True)
        _restart_server(sccache, env)

        second_payload = _build(cmake, sccache, cxx, build_dir, env)
        second = _counters(second_payload)
        print("::group::second build (expected: a hit, served from S3)")
        print(f"  requests: {_counter(second, 'compile_requests')}")
        print(f"  hits: {_counter(second, 'cache_hits')}  misses: {_counter(second, 'cache_misses')}")
        print(f"  errors: {_error_total(second)}")
        print("::endgroup::")
        # Without a daemon, --show-stats prints zeroed counters that would pass.
        if _counter(second, "compile_requests") != 1:
            fail(
                "second-build/no-request",
                "the second build did not reach the sccache daemon -- these stats describe nothing",
            )
        if _error_total(second):
            fail("second-build/cache-errors", "sccache reported cache errors on the second build")
        if _counter(second, "cache_misses"):
            fail(
                "read/miss",
                "the second build missed: either nothing was stored, or the read from the object store failed",
            )
        if not _counter(second, "cache_hits"):
            fail("read/no-hit", "the second build was not a cache hit -- nothing was read back from the object store")
        if _backend_of(second_payload) == "local-disk":
            fail("read/local-disk", "the second build was served by a local disk cache, not the object store")
    except Exception as exc:  # noqa: BLE001 -- the message is the whole output
        fail(f"probe/{type(exc).__name__}", f"{type(exc).__name__}: {exc}")
    finally:
        _run([sccache, "--stop-server"], env, check=False)
        if args.keep:
            print(f"--keep: leaving {len(_list_keys(client, bucket, key_prefix))} objects and {tmp}")
        else:
            try:
                removed = _delete_prefix(client, bucket, key_prefix)
                print(f"cleaned up {removed} cache objects")
            except Exception as exc:  # noqa: BLE001 -- cleanup must not mask a real failure
                print(f"::warning::could not clean up this run's objects: {exc}")
            shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        _fail("sccache probe failed: " + "; ".join(failures))
        return 1
    print("sccache round trip through the object store: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
