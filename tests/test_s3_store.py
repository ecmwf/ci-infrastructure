# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the S3 artifact store backend.

These exercise the store logic against an in-memory fake S3 client (injected
via the `client=` parameter), so they run with no network and no credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from ci_infrastructure import s3_store
from ci_infrastructure._errors import CIError


@pytest.fixture(autouse=True)
def _configured_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the now-required object-store location for every test.

    The module has no built-in endpoint/bucket default; the deployment provides
    them. Tests that assert on the missing-config behaviour delete these again.
    """
    monkeypatch.setenv("ARTIFACT_S3_ENDPOINT", "https://s3.test.invalid")
    monkeypatch.setenv("ARTIFACT_S3_BUCKET", "test-bucket")


class FakeS3:
    """Minimal stand-in for a boto3 S3 client: an in-memory key -> bytes map."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    @staticmethod
    def _missing(op: str) -> ClientError:
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, op)

    def head_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803 (boto3 kwargs)
        if Key not in self.objects:
            raise self._missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        if Key not in self.objects:
            raise self._missing("GetObject")
        Path(Filename).write_bytes(self.objects[Key])

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, object]:  # noqa: N803
        contents = [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]
        return {"Contents": contents}


@pytest.fixture
def fake() -> FakeS3:
    return FakeS3()


def test_artifact_key_default_prefix() -> None:
    assert s3_store.artifact_key("fortmath-abc-ubuntu-24.04-gfortran-14-Release") == (
        "fortmath-abc-ubuntu-24.04-gfortran-14-Release.tar.gz"
    )


def test_artifact_key_honours_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTIFACT_S3_KEY_PREFIX", "artifacts/")
    assert s3_store.artifact_key("cxxmath-deadbeef-x") == "artifacts/cxxmath-deadbeef-x.tar.gz"


def test_download_creates_the_destination_parent_directory(fake: FakeS3, tmp_path: Path) -> None:
    """download() mkdir -p's dest.parent, so a caller may name a path whose
    directory does not exist yet -- which is what fetch_deps does per dep."""
    src = tmp_path / "src.tar.gz"
    src.write_bytes(b"the-install-tree")
    s3_store.upload("pkg-1", src, client=fake)

    dest = tmp_path / "not" / "yet" / "there" / "out.tar.gz"
    assert s3_store.download("pkg-1", dest, client=fake) is True
    assert dest.parent.is_dir()


def test_download_missing_returns_false(fake: FakeS3, tmp_path: Path) -> None:
    assert s3_store.download("absent", tmp_path / "x.tar.gz", client=fake) is False


def test_non_404_client_error_propagates(tmp_path: Path) -> None:
    class Denied(FakeS3):
        def head_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "HeadObject")

    with pytest.raises(ClientError):
        s3_store.object_exists("pkg", client=Denied())


def test_list_with_prefix_strips_suffix(fake: FakeS3, tmp_path: Path) -> None:
    for name in ("cxxmath-a", "cxxmath-b", "fortmath-c"):
        tar = tmp_path / f"{name}.tar.gz"
        tar.write_bytes(b"x")
        s3_store.upload(name, tar, client=fake)

    found = s3_store.list_with_prefix("cxxmath", client=fake)
    assert found == ["cxxmath-a", "cxxmath-b"]


def test_list_with_prefix_respects_limit(fake: FakeS3, tmp_path: Path) -> None:
    for i in range(5):
        tar = tmp_path / f"pkg-{i}.tar.gz"
        tar.write_bytes(b"x")
        s3_store.upload(f"pkg-{i}", tar, client=fake)
    assert len(s3_store.list_with_prefix("pkg", limit=3, client=fake)) == 3


def test_ca_bundle_explicit_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = tmp_path / "custom-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("ARTIFACT_S3_CA_BUNDLE", str(bundle))
    assert s3_store._ca_bundle() == bundle


def test_ca_bundle_explicit_missing_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARTIFACT_S3_CA_BUNDLE", str(tmp_path / "nope.pem"))
    with pytest.raises(FileNotFoundError):
        s3_store._ca_bundle()


def test_ca_bundle_defaults_to_vendored_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTIFACT_S3_CA_BUNDLE", raising=False)
    assert s3_store._ca_bundle() == s3_store._VENDORED_ROOT_CA


def test_vendored_root_ca_is_shipped_and_parses() -> None:
    # Package data must actually be present and be a self-signed root for the
    # object store (HARICA TLS RSA Root CA 2021), not just any file.
    pem = s3_store._VENDORED_ROOT_CA.read_text()
    assert "HARICA TLS RSA Root CA 2021" in pem
    assert "-----BEGIN CERTIFICATE-----" in pem


def test_bucket_required_when_unset(monkeypatch: pytest.MonkeyPatch, fake: FakeS3) -> None:
    monkeypatch.delenv("ARTIFACT_S3_BUCKET", raising=False)
    with pytest.raises(CIError, match="ARTIFACT_S3_BUCKET is not set"):
        s3_store.object_exists("pkg", client=fake)


def test_endpoint_required_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # No injected client, so _client() builds a real boto3 client and must first
    # read the (now required) endpoint.
    monkeypatch.delenv("ARTIFACT_S3_ENDPOINT", raising=False)
    with pytest.raises(CIError, match="ARTIFACT_S3_ENDPOINT is not set"):
        s3_store.object_exists("pkg")


def test_ca_bundle_raises_when_none_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTIFACT_S3_CA_BUNDLE", raising=False)
    monkeypatch.setattr(s3_store.Path, "is_file", lambda self: False)  # type: ignore[attr-defined]
    with pytest.raises(FileNotFoundError):
        s3_store._ca_bundle()
