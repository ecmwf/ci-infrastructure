# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""upload-nexus against a local fake Nexus: at most once, only tested bytes, test hosts only."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ci_infrastructure.release import ReleaseError, nexus

FILENAME = "pkg-2.5.0~rc1-1.x86_64.rpm"


class _FakeNexus(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    fail_next: list[int] = []
    requests: list[tuple[str, str, str | None]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self) -> None:
        self.requests.append((self.command, self.path, self.headers.get("Authorization")))
        if self.fail_next:
            self._reply(self.fail_next.pop(0))
            return
        if self.command in ("GET", "HEAD"):
            body = self.files.get(self.path)
            self._reply(404) if body is None else self._reply(200, body)
        elif self.command == "PUT":
            self.files[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
            self._reply(201)

    do_GET = do_HEAD = do_PUT = _handle


@pytest.fixture
def fake() -> Iterator[tuple[str, type[_FakeNexus]]]:
    _FakeNexus.files, _FakeNexus.fail_next, _FakeNexus.requests = {}, [], []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeNexus)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/repository/rpm", _FakeNexus
    server.shutdown()


def _package(tmp_path: Path, content: bytes = b"rpm-bytes") -> tuple[Path, str]:
    path = tmp_path / FILENAME
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _upload(path: Path, base: str, digest: str) -> dict[str, Any]:
    return nexus.upload(
        path, base_url=base, expected_filename=FILENAME, tested_sha256=digest, auth="u:secret", sleep=lambda s: None
    )


def test_absent_is_uploaded_and_verified(tmp_path: Path, fake: tuple[str, type[_FakeNexus]]) -> None:
    base, server = fake
    path, digest = _package(tmp_path)
    receipt = _upload(path, base, digest)
    assert (receipt["action"], receipt["sha256"]) == ("uploaded", digest)
    assert receipt["url"] == f"{base}/{FILENAME}"
    assert server.files["/repository/rpm/" + FILENAME] == b"rpm-bytes"
    assert [r[0] for r in server.requests] == ["GET", "PUT", "GET"]
    assert all(r[2] == "Basic dTpzZWNyZXQ=" for r in server.requests)


def test_the_same_bytes_are_a_no_op(tmp_path: Path, fake: tuple[str, type[_FakeNexus]]) -> None:
    base, server = fake
    path, digest = _package(tmp_path)
    server.files["/repository/rpm/" + FILENAME] = b"rpm-bytes"
    assert _upload(path, base, digest)["action"] == "already-present"
    assert [r[0] for r in server.requests] == ["GET"]


def test_other_bytes_fail_and_are_kept(tmp_path: Path, fake: tuple[str, type[_FakeNexus]]) -> None:
    base, server = fake
    path, digest = _package(tmp_path)
    server.files["/repository/rpm/" + FILENAME] = b"rebuilt"
    with pytest.raises(ReleaseError, match="versions are immutable; bump rc"):
        _upload(path, base, digest)
    assert server.files["/repository/rpm/" + FILENAME] == b"rebuilt"


def test_a_server_error_is_retried(tmp_path: Path, fake: tuple[str, type[_FakeNexus]]) -> None:
    base, server = fake
    path, digest = _package(tmp_path)
    server.fail_next = [500]
    assert _upload(path, base, digest)["action"] == "uploaded"
    assert [r[0] for r in server.requests] == ["GET", "GET", "PUT", "GET"]


def test_only_the_tested_bytes_are_uploaded(tmp_path: Path, fake: tuple[str, type[_FakeNexus]]) -> None:
    base, server = fake
    path, _ = _package(tmp_path)
    with pytest.raises(ReleaseError, match="not what the install test passed"):
        _upload(path, base, "0" * 64)
    assert server.requests == []


def test_the_file_name_must_be_the_expected_one(tmp_path: Path, fake: tuple[str, type[_FakeNexus]]) -> None:
    base, _ = fake
    path = tmp_path / "other.rpm"
    path.write_bytes(b"x")
    with pytest.raises(ReleaseError, match="not the expected"):
        _upload(path, base, hashlib.sha256(b"x").hexdigest())


def test_probe_and_already_published(fake: tuple[str, type[_FakeNexus]]) -> None:
    base, server = fake
    assert nexus.probe(base, FILENAME) == "absent"
    server.files["/repository/rpm/" + FILENAME] = b"rpm-bytes"
    assert nexus.probe(base, FILENAME) == "present"
    assert server.requests[-1][2] is None  # anonymous
    receipt = nexus.already_published(base_url=base, filename=FILENAME, auth="u:p", sleep=lambda s: None)
    assert (receipt["action"], receipt["sha256"]) == ("already-published", hashlib.sha256(b"rpm-bytes").hexdigest())
    server.fail_next = [503]
    assert nexus.probe(base, FILENAME) == "unknown"


@pytest.mark.parametrize(
    ("url", "allow_production", "match"),
    [
        ("https://nexus.ecmwf.int/repository/rpm", False, "not a test Nexus host"),
        ("http://nexus-test.ecmwf.int/repository/rpm", False, "must be https"),
        ("https://u:p@nexus-test.ecmwf.int/repository/rpm", False, "credentials"),
        ("https://evil.example/repository/rpm", True, "not a Nexus host"),
        ("https://nexus-test.ecmwf.int:8443/repository/rpm", False, "not a test Nexus host"),
        ("https://nexus-test.ecmwf.int.evil.example/repository/rpm", False, "not a test Nexus host"),
    ],
)
def test_check_url_refuses(url: str, allow_production: bool, match: str) -> None:
    with pytest.raises(ReleaseError, match=match):
        nexus.check_url(url, allow_production=allow_production)


def test_check_url_accepts_test_and_with_allow_production_prod() -> None:
    assert (
        nexus.check_url("https://nexus-test.ecmwf.int/repository/rpm/") == "https://nexus-test.ecmwf.int/repository/rpm"
    )
    assert nexus.check_url("https://nexus.ecmwf.int/r", allow_production=True) == "https://nexus.ecmwf.int/r"


@pytest.mark.parametrize(
    "secret",
    [
        "https://nexus-test.ecmwf.int/repository/rpm",
        "https://nexus-test.ecmwf.int/repository/rpm/",
        "https://Nexus-Test.ecmwf.int/repository/rpm",
        "https://nexus-test.ecmwf.int:443/repository/rpm",
        "https://nexus-test.ecmwf.int/repository/rpm\n",
    ],
)
def test_a_v2_url_secret_works_as_the_base_url(secret: str) -> None:
    """The URL is v2's NEXUS_TEST_REPO_URL_* secret, which v2 PUTs ``<url>/<file>`` to; the
    workflow builds the JSON around it, so a trailing newline lands raw inside the string."""
    urls = nexus.parse_urls('{"rocky-9": "' + secret + '"}', what="--urls")
    base = nexus.target_url(urls, "rocky-9", allow_production=False)
    assert nexus.file_url(base, FILENAME) == secret.strip().rstrip("/") + "/" + FILENAME


def test_a_masked_url_is_not_echoed_in_errors() -> None:
    with pytest.raises(ReleaseError) as exc:
        nexus.check_url("http://nexus-test.ecmwf.int/secret-repo")
    assert "secret-repo" not in str(exc.value)


@pytest.mark.parametrize(("text", "match"), [("[]", "JSON object"), ('{"t": "a"b"}', "not valid JSON")])
def test_parse_urls_refuses(text: str, match: str) -> None:
    with pytest.raises(ReleaseError, match=match):
        nexus.parse_urls(text, what="--urls")


def test_file_url_quotes_and_refuses_paths() -> None:
    assert nexus.file_url("https://h/r", "a b~1.rpm") == "https://h/r/a%20b~1.rpm"
    with pytest.raises(ReleaseError, match="plain file name"):
        nexus.file_url("https://h/r", "../x.rpm")


def test_cli_refuses_a_prod_url_and_an_untested_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_AUTH", "u:p")
    args = ["--target", "rocky-9", "--filename", FILENAME, "--leg-id", "l", "--receipt-dir", str(tmp_path)]
    prod = json.dumps({"rocky-9": "https://nexus.ecmwf.int/repository/rpm"})
    result = CliRunner().invoke(nexus.main, ["--urls", prod, *args, "--state", "already-published"])
    assert result.exit_code != 0 and "not a test Nexus host" in result.output
    itest = tmp_path / "itest.json"
    itest.write_text(json.dumps({"filename": FILENAME, "result": "failed", "sha256": "x"}))
    test = json.dumps({"rocky-9": "https://nexus-test.ecmwf.int/repository/rpm"})
    result = CliRunner().invoke(
        nexus.main, ["--urls", test, *args, "--package-dir", str(tmp_path), "--itest", str(itest)]
    )
    assert result.exit_code != 0 and "needs --package-dir, --itest, --sha and --job-key" in result.output
    build = ["--package-dir", str(tmp_path), "--itest", str(itest), "--sha", "a" * 40, "--job-key", "k"]
    result = CliRunner().invoke(nexus.main, ["--urls", test, *args, *build])
    assert result.exit_code != 0 and "not a passed install test" in result.output
    # A passed test, but of another commit's (or job's) build.
    for sha, key in (("c" * 40, "k"), ("a" * 40, "other")):
        itest.write_text(
            json.dumps({"filename": FILENAME, "result": "passed", "sha256": "x", "sha": sha, "job_key": key})
        )
        result = CliRunner().invoke(nexus.main, ["--urls", test, *args, *build])
        assert result.exit_code != 0 and "tested the build of commit" in result.output, result.output
    monkeypatch.delenv("NEXUS_AUTH")
    result = CliRunner().invoke(nexus.main, ["--urls", test, *args, "--state", "already-published"])
    assert result.exit_code != 0 and "NEXUS_AUTH is empty" in result.output
