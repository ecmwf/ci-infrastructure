# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""S3 artifact store, against an in-memory fake client."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from ci_infrastructure import s3_store
from ci_infrastructure._errors import CIError


@pytest.fixture(autouse=True)
def _configured_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTIFACT_S3_ENDPOINT", "https://s3.test.invalid")
    monkeypatch.setenv("ARTIFACT_S3_BUCKET", "test-bucket")


NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


class FakeS3:
    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.written: dict[str, datetime] = {}
        self.page_size = page_size
        self.versioning: dict[str, str] = {}

    @staticmethod
    def _missing(op: str) -> ClientError:
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, op)

    def head_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803 (boto3 kwargs)
        if Key not in self.objects:
            raise self._missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:  # noqa: N803
        self.put(Key, Path(Filename).read_bytes(), NOW)

    def put(self, key: str, data: bytes, written: datetime) -> None:
        self.objects[key] = data
        self.written[key] = written

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        del self.objects[Key], self.written[Key]

    def get_bucket_versioning(self, Bucket: str) -> dict[str, str]:  # noqa: N803
        return self.versioning

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        if Key not in self.objects:
            raise self._missing("GetObject")
        Path(Filename).write_bytes(self.objects[Key])

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        if Key not in self.objects:
            raise self._missing("GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.put(Key, Body, NOW)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, object]:
        keys = [k for k in sorted(self.objects) if k.startswith(kwargs["Prefix"])]
        start = int(kwargs.get("ContinuationToken", 0))
        page = keys[start : start + self.page_size]
        contents = [{"Key": k, "Size": len(self.objects[k]), "LastModified": self.written[k]} for k in page]
        more = start + self.page_size < len(keys)
        return {"Contents": contents, "IsTruncated": more, "NextContinuationToken": str(start + self.page_size)}


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
    pem = s3_store._VENDORED_ROOT_CA.read_text()
    assert "HARICA TLS RSA Root CA 2021" in pem
    assert "-----BEGIN CERTIFICATE-----" in pem


def test_bucket_required_when_unset(monkeypatch: pytest.MonkeyPatch, fake: FakeS3) -> None:
    monkeypatch.delenv("ARTIFACT_S3_BUCKET", raising=False)
    with pytest.raises(CIError, match="ARTIFACT_S3_BUCKET is not set"):
        s3_store.object_exists("pkg", client=fake)


def test_endpoint_required_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTIFACT_S3_ENDPOINT", raising=False)
    with pytest.raises(CIError, match="ARTIFACT_S3_ENDPOINT is not set"):
        s3_store.object_exists("pkg")


def test_ca_bundle_raises_when_none_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTIFACT_S3_CA_BUNDLE", raising=False)
    monkeypatch.setattr(s3_store.Path, "is_file", lambda self: False)  # type: ignore[attr-defined]
    with pytest.raises(FileNotFoundError):
        s3_store._ca_bundle()


def test_list_objects_follows_pagination() -> None:
    fake = FakeS3(page_size=2)
    for i in range(5):
        fake.put(f"a{i}.tar.gz", b"x", NOW)
    assert [o.key for o in s3_store.list_objects(client=fake)] == [f"a{i}.tar.gz" for i in range(5)]


def test_artifact_index_lists_each_prefix_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTIFACT_S3_KEY_PREFIX", "ci/")
    fake = FakeS3(page_size=1)
    for name in ("eckit-abc-x-ubuntu", "eckit-abc-x-rocky", "eckit-geo-abc-x-ubuntu"):
        fake.put(f"ci/{name}.tar.gz", b"x", NOW)
    index = s3_store.ArtifactIndex(client=fake)
    assert index.exists("eckit-abc-x-ubuntu", prefix="eckit-abc-")
    assert index.exists("eckit-abc-x-rocky", prefix="eckit-abc-")
    assert not index.exists("eckit-abc-y-rocky", prefix="eckit-abc-")
    assert not index.exists("eckit-geo-abc-x-rocky", prefix="eckit-geo-abc-")
    assert index.listings == 2


def _obj(key: str, size: int, days_ago: float) -> s3_store.StoredObject:
    return s3_store.StoredObject(key, size, NOW - timedelta(days=days_ago))


def test_oldest_written_go_first_until_the_target() -> None:
    objects = [_obj("new.tar.gz", 31, 2), _obj("oldest.tar.gz", 20, 9), _obj("old.tar.gz", 20, 5)]
    assert [o.key for o in s3_store.eviction_plan(objects, target=50)] == ["oldest.tar.gz", "old.tar.gz"]


def test_nothing_goes_at_or_below_the_target() -> None:
    assert s3_store.eviction_plan([_obj("a.tar.gz", 30, 30), _obj("b.tar.gz", 20, 20)], target=50) == []


def test_only_suffix_objects_go_but_everything_counts() -> None:
    plan = s3_store.eviction_plan([_obj("notes.txt", 60, 30), _obj("a.tar.gz", 1, 2)], target=50)
    assert [o.key for o in plan] == ["a.tar.gz"]
    plan = s3_store.eviction_plan([_obj("notes.txt", 60, 30), _obj("a.tar.gz", 1, 2)], target=50, suffix="")
    assert [o.key for o in plan] == ["notes.txt"]


def _fill(fake: FakeS3) -> None:
    for i, days in enumerate((9, 7, 5, 3)):
        fake.put(f"a{i}.tar.gz", b"x" * 10, NOW - timedelta(days=days))


def test_gc_without_a_quota_halves_current_usage(fake: FakeS3) -> None:
    _fill(fake)
    s3_store.gc(percent=50, quota=None, dryrun=False, client=fake)
    assert sorted(fake.objects) == ["a2.tar.gz", "a3.tar.gz"]


def test_gc_against_a_quota(fake: FakeS3) -> None:
    _fill(fake)
    s3_store.gc(percent=50, quota=60, dryrun=False, client=fake)
    assert sorted(fake.objects) == ["a1.tar.gz", "a2.tar.gz", "a3.tar.gz"]


def test_gc_dryrun_deletes_nothing(fake: FakeS3) -> None:
    _fill(fake)
    plan = s3_store.gc(percent=50, quota=None, dryrun=True, client=fake)
    assert [o.key for o in plan] == ["a0.tar.gz", "a1.tar.gz"]
    assert len(fake.objects) == 4


def test_gc_warns_when_versioning_is_on(fake: FakeS3, capsys: pytest.CaptureFixture[str]) -> None:
    fake.versioning = {"Status": "Enabled"}
    s3_store.gc(percent=50, quota=None, dryrun=True, client=fake)
    assert "frees nothing" in capsys.readouterr().out


def test_gc_deletes_the_sidecars_left_without_their_artifact(fake: FakeS3) -> None:
    _fill(fake)
    for stem in ("a0", "a3", "orphan"):
        fake.put(f"{stem}.deps.json", b"", NOW - timedelta(days=2))
    s3_store.gc(percent=50, quota=None, dryrun=False, client=fake)
    assert sorted(fake.objects) == ["a2.tar.gz", "a3.deps.json", "a3.tar.gz"]


def test_a_fresh_sidecar_waits_for_its_artifact() -> None:
    objects = [_obj("building.deps.json", 2, 0.5), _obj("abandoned.deps.json", 2, 3)]
    assert [o.key for o in s3_store.stale_sidecars(objects, [], NOW)] == ["abandoned.deps.json"]


def test_artifact_index_offers_the_recorded_builds_newest_first(fake: FakeS3) -> None:
    for name, days in (("eckit-abc-old", 3), ("eckit-abc-new", 1), ("eckit-abc-bare", 0)):
        fake.put(f"{name}.tar.gz", b"x", NOW - timedelta(days=days))
    for name in ("eckit-abc-old", "eckit-abc-new", "eckit-abc-gone"):
        fake.put(f"{name}.deps.json", b'{"direct": []}', NOW)
    index = s3_store.ArtifactIndex(client=fake)
    assert index.with_sidecar("eckit-abc-") == ["eckit-abc-new", "eckit-abc-old"]
    assert index.read_sidecar("eckit-abc-new") == {"direct": []}


def test_artifact_index_writes_only_the_missing_sidecars(fake: FakeS3) -> None:
    fake.put("eckit-abc-x.deps.json", b"kept", NOW)
    index = s3_store.ArtifactIndex(client=fake)
    index.record_sidecar("eckit-abc-x", "eckit-abc-", {"direct": ["new"]})
    index.record_sidecar("eckit-abc-y", "eckit-abc-", {"direct": []})
    assert index.flush_sidecars() == 1
    assert fake.objects["eckit-abc-x.deps.json"] == b"kept"
    assert json.loads(fake.objects["eckit-abc-y.deps.json"]) == {"direct": []}
