#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Compile a small C++ project twice and prove the second compile was served from
the object store, so a broken sccache backend fails here rather than silently
costing every downstream build its cache.

Run it by hand exactly as CI runs it -- that is the point of it being a script
rather than bash inside a workflow:

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

With --use-ambient-config the SCCACHE_* variables are taken as they already
stand, which is how CI runs it after actions/setup-sccache: that way the CI
path tests the action too, while a laptop needs none of it.

WHY THREE CHECKS AND NOT "COMPILE TWICE, EXPECT A HIT":

sccache fails *soft*. If its S3 backend cannot be reached -- wrong bucket, no
credentials, a TLS root the host does not trust -- it does not error out, it
quietly falls back to a local disk cache. A naive second-compile-is-a-hit test
therefore passes against a completely dead bucket. So:

    1. backend identity  sccache must report an S3 backend, not local disk
    2. write proof       objects must appear in THAT bucket, per boto3
    3. read proof        with the local cache erased, a hit can only be remote

Check 2 proves the write, check 3 proves the read, check 1 explains a failure
of either.

Nothing is written outside a temporary directory, and every object is stored
under a key prefix unique to this run, so a hand run cannot disturb CI or
another developer's cache. The prefix is deleted on the way out unless --keep.

No secret value is printed: this repository is public and the endpoint and
bucket are both held as secrets. Configuration is identified by a truncated
digest only, which is enough to check that a hand run and a CI run were
pointed at the same place.
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

# Where the two-line C++ project lives, relative to the repository root.
_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples" / "sccache"

# Tried in order when CXX is unset. clang++-18 first because that is what the
# ubuntu24.04-clang18-gfortran13 image ships and what the artifact names encode;
# the bare names are so a laptop works without configuration.
_COMPILER_CANDIDATES = ("clang++-18", "clang++", "g++")

# A port of our own, so a hand run neither talks to nor kills the developer's
# already-running sccache server (which would have entirely different config).
_DEFAULT_SERVER_PORT = "4237"


def _env(name: str) -> str:
    "Environment value with surrounding whitespace removed."
    return os.environ.get(name, "").strip()


def _fingerprint(value: str) -> str:
    """Short digest of a value, for comparing environments without printing it.

    Same value in two places -> same digest, so a CI run and a hand run can be
    told apart from two runs pointed at different buckets.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "<unset>"


def _fail(message: str) -> None:
    "Emit a CI-annotated error. Harmless noise in a terminal, a red line in CI."
    print(f"::error::{message}")


def _run(cmd: list[str], env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command with the probe's environment, capturing output.

    Output is captured rather than streamed so a successful run stays quiet and
    a failing one can be printed in full inside its own group.
    """
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        print(f"::group::failed: {' '.join(cmd[:2])}")
        print(proc.stdout)
        print(proc.stderr)
        print("::endgroup::")
        raise RuntimeError(f"command failed with exit code {proc.returncode}: {cmd[0]}")
    return proc


def _which_or_fail(tool: str) -> str:
    "Absolute path to a required tool, or a clear error naming what is missing."
    found = shutil.which(tool)
    if not found:
        raise RuntimeError(f"{tool} is not on PATH -- install it, or run this where it is available")
    return found


def _pick_compiler() -> str:
    "CXX if set, else the first candidate present. Errors name every candidate."
    explicit = _env("CXX")
    if explicit:
        return _which_or_fail(explicit)
    for candidate in _COMPILER_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError(f"no C++ compiler found -- set CXX, or install one of: {', '.join(_COMPILER_CANDIDATES)}")


def _counter(stats: dict[str, Any], key: str) -> int:
    """Read one sccache counter, tolerating both stats shapes.

    sccache reports some counters as a plain integer and others as
    ``{"counts": {"C/C++": 1}}`` depending on version. Summing the inner map
    rather than assuming one shape keeps this working across sccache upgrades
    instead of silently reading zero and asserting nothing.
    """
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
    """The whole `--show-stats --stats-format=json` document.

    Whole, not just "stats": the backend sits at the top level as
    "cache_location" and the counters under "stats", and callers need both.
    """
    proc = _run([sccache, "--show-stats", "--stats-format=json"], env)
    payload = json.loads(proc.stdout)
    return payload if isinstance(payload, dict) else {}


def _counters(payload: dict[str, Any]) -> dict[str, Any]:
    "The counter mapping, which some versions nest under 'stats' and some do not."
    stats = payload.get("stats", payload)
    return stats if isinstance(stats, dict) else {}


# Every scheme sccache can render as a remote. Anything else is either the disk
# cache or wording we do not recognise, and both must fail rather than be
# guessed at.
_REMOTE_SCHEMES = frozenset({"s3", "gcs", "azblob", "ghac", "redis", "webdav", "oss", "memcached", "cos"})


def _backend_of(payload: dict[str, Any]) -> str:
    """The backend NAME only -- never the location string, which holds a secret.

    sccache renders an opendal remote as ``<scheme>, name: <bucket>, prefix:
    <root>``, the disk cache as ``Local disk: "<path>"`` and a chain as
    ``Multi-level (<n> levels)``. The bucket name is in there verbatim and is a
    secret in this deployment, so only the leading scheme is ever returned and
    therefore only the scheme can ever be printed.

    A read-only remote still reports its scheme -- the read-only wrapper
    delegates -- so the scheme alone never proves the cache is writable. That
    is what the cache_writes assertion is for.
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
    """A boto3 client built exactly like s3_store's, vendored CA included.

    The same construction as the artifact store means what passes here is what
    the real publish path will do.
    """
    return boto3.client(
        "s3",
        endpoint_url=_env("SCCACHE_ENDPOINT") or _env("ARTIFACT_S3_ENDPOINT"),
        region_name=_env("ARTIFACT_S3_REGION") or "RegionOne",
        verify=str(s3_store._ca_bundle()),
    )


# Written by sccache's own startup write-probe, not by any compile. Counting it
# would let "objects exist under our prefix" pass with zero compilations cached.
_STARTUP_PROBE_KEY = ".sccache_check"


def _list_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    "Every key under a prefix. Paginated: sccache writes more than one object."
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
    "Remove everything this run wrote, so repeated probes do not fill the bucket."
    keys = _list_keys(client, bucket, prefix)
    for start in range(0, len(keys), 1000):
        batch = keys[start : start + 1000]
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})
    return len(keys)


def _sccache_env(base: dict[str, str], key_prefix: str, cache_dir: Path, ambient: bool) -> dict[str, str]:
    """The environment the sccache server will capture when it starts.

    In ambient mode the SCCACHE_* values are left as the caller (in CI, the
    setup-sccache action) established them, so the action itself is under test.
    Otherwise they are derived here from the artifact-store names, which is what
    lets a hand run be a single command.
    """
    env = dict(base)
    if not ambient:
        endpoint = _env("SCCACHE_ENDPOINT") or _env("ARTIFACT_S3_ENDPOINT")
        env["SCCACHE_ENDPOINT"] = endpoint
        # Required by sccache; it does NOT read AWS_REGION for this.
        env["SCCACHE_REGION"] = _env("SCCACHE_REGION") or _env("ARTIFACT_S3_REGION") or "RegionOne"
        env["SCCACHE_S3_USE_SSL"] = _env("ARTIFACT_S3_USE_SSL") or ("true" if endpoint.startswith("https") else "false")

    # Always ours, in both modes. The prefix must be unique per run or a
    # previous run's entries make the FIRST compile a hit and the probe proves
    # nothing; the cache dir must be ours so erasing it cannot touch the
    # developer's real cache; the port must be ours so we do not adopt or kill
    # an unrelated sccache server that is running with different config.
    env["SCCACHE_S3_KEY_PREFIX"] = key_prefix
    env["SCCACHE_DIR"] = str(cache_dir)
    env.setdefault("SCCACHE_SERVER_PORT", _DEFAULT_SERVER_PORT)
    # Virtual-host addressing must stay off: the store has no wildcard DNS, and
    # its certificate is a single-label wildcard that would not match anyway.
    env.pop("SCCACHE_S3_ENABLE_VIRTUAL_HOST_STYLE", None)
    # A multi-level chain would put a local disk tier in FRONT of S3, so the
    # second build would be served from that tier and the read proof would
    # prove nothing. Drop it rather than quietly measure the wrong thing.
    env.pop("SCCACHE_MULTILEVEL_CHAIN", None)
    return env


def _restart_server(sccache: str, env: dict[str, str]) -> None:
    """Stop then start the server, so it captures the config we just built.

    The server reads its configuration once, at startup, and the install action
    may already have started one with no S3 settings at all. Without this the
    probe would measure whatever that stale daemon was configured with.
    """
    _run([sccache, "--stop-server"], env, check=False)
    _run([sccache, "--start-server"], env)


def _build(cmake: str, sccache: str, cxx: str, build_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    """Configure, zero the stats, then build. Returns the stats for the build.

    Configure happens BEFORE zeroing on purpose: CMake's own compiler probes
    compile throwaway files through the launcher too, and counting those would
    let a build register a "hit" that had nothing to do with our source.
    """
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
        # Built here, before anything is started or written, so a missing CA
        # bundle or a malformed endpoint fails as a message rather than as a
        # traceback out of the cleanup block.
        client = _s3_client()
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")
        return 1

    # Unique per run. In ambient mode it extends whatever namespace the action
    # established rather than replacing it, so the action's prefix is still
    # exercised while this run still gets a slot nobody has written to.
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
    tmp = Path(tempfile.mkdtemp(prefix="sccache-probe-"))
    build_dir = tmp / "build"
    cache_dir = tmp / "local-cache"
    env = _sccache_env(dict(os.environ), key_prefix, cache_dir, args.use_ambient_config)

    try:
        _restart_server(sccache, env)

        # 1. Backend identity. sccache chooses ONE backend when the daemon
        #    starts and never revisits it; if SCCACHE_BUCKET was missing from
        #    that moment's environment it silently uses a local disk cache, and
        #    every check after this one would still look healthy.
        payload = _show_stats(sccache, env)
        print("::group::sccache backend")
        # Dumped because the schema has moved between versions: if a counter
        # below ever reads zero for the wrong reason, this is the evidence
        # needed to fix it. cache_location is stripped -- it spells out the
        # bucket, which is a secret, and this repository is public.
        print(json.dumps(_redacted(payload), indent=2, sort_keys=True))
        print("::endgroup::")
        backend = _backend_of(payload)
        print(f"  backend: {backend or '<not reported>'}")
        if backend == "local-disk":
            failures.append("backend/local-disk")
            _fail(
                "sccache is using a LOCAL DISK cache, not the object store: the daemon was started before "
                "SCCACHE_BUCKET was in its environment, so every later check would pass against a dead bucket"
            )
        elif backend and backend != "s3":
            failures.append(f"backend/{backend}")
            _fail(f"sccache reports a {backend!r} backend, not s3")

        # 2. Write proof: objects in THAT bucket, seen by a different client
        #    over a different TLS trust path.
        first = _counters(_build(cmake, sccache, cxx, build_dir, env))
        print("::group::first build (expected: a miss, and a write)")
        print(f"  requests: {_counter(first, 'compile_requests')}")
        print(f"  misses: {_counter(first, 'cache_misses')}  hits: {_counter(first, 'cache_hits')}")
        print(f"  writes: {_counter(first, 'cache_writes')}  errors: {_error_total(first)}")
        print("::endgroup::")
        if _error_total(first):
            failures.append("first-build/cache-errors")
            _fail("sccache reported cache errors on the first build")
        if not _counter(first, "cache_misses"):
            failures.append("first-build/no-miss")
            _fail("the first build was not a cache miss -- the key prefix was not fresh")
        if not _counter(first, "cache_writes"):
            # A bucket that reads but refuses writes leaves sccache in
            # read-only mode, still reporting an s3 backend.
            failures.append("first-build/no-write")
            _fail("sccache cached nothing -- the bucket accepted reads but not writes")

        # The startup write-probe object is not a cached compilation, so it must
        # not be what satisfies this check.
        written = [k for k in _list_keys(client, bucket, key_prefix) if not k.endswith(_STARTUP_PROBE_KEY)]
        print("::group::objects written to the bucket")
        print(f"  cached objects under this run's prefix: {len(written)}")
        print("::endgroup::")
        if not written:
            failures.append("write/no-objects")
            _fail("sccache wrote nothing to the bucket -- the compile was cached somewhere else")

        # 3. Read proof: erase every local trace, then rebuild. A hit now cannot
        #    have come from disk, so it came from the object store.
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
        # Load-bearing: with no daemon running at all, --show-stats does not
        # start one -- it rebuilds the description from config and prints a
        # plausible s3 location with every counter at zero. Without this, that
        # reads as "no errors, no misses" and passes.
        if _counter(second, "compile_requests") != 1:
            failures.append("second-build/no-request")
            _fail("the second build did not reach the sccache daemon -- these stats describe nothing")
        if _error_total(second):
            failures.append("second-build/cache-errors")
            _fail("sccache reported cache errors on the second build")
        if _counter(second, "cache_misses"):
            # A mid-run S3 read failure is reported as an ordinary miss, so a
            # miss here is not merely "not cached" -- it may be a broken store.
            failures.append("read/miss")
            _fail("the second build missed: either nothing was stored, or the read from the object store failed")
        if not _counter(second, "cache_hits"):
            failures.append("read/no-hit")
            _fail("the second build was not a cache hit -- nothing was read back from the object store")
        if _backend_of(second_payload) == "local-disk":
            failures.append("read/local-disk")
            _fail("the second build was served by a local disk cache, not the object store")
    except Exception as exc:  # noqa: BLE001 -- the message is the whole output
        failures.append(f"probe/{type(exc).__name__}")
        _fail(f"{type(exc).__name__}: {exc}")
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
