# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from conftest import stub_gh
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ci_infrastructure import _github_api, app_token


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_the_jwt_is_signed_with_the_app_key() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    header, payload, signature = app_token.app_jwt("Iv23client", pem, now=1_000_000).split(".")
    key.public_key().verify(_unb64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(_unb64(payload)) == {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23client"}


def test_without_credentials_nothing_is_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(app_token.CLIENT_ID_ENV, raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "ecmwf")
    assert app_token.mint_from_env() is None


def test_an_expired_token_is_replaced_for_good(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "tokens"
    gh = (
        f'echo "$GH_TOKEN" >> {log}\n'
        'if [ "$GH_TOKEN" = old ]; then echo "gh: Bad credentials (HTTP 401)" >&2; exit 1; fi\n'
        "echo '{}'"
    )
    monkeypatch.setenv("PATH", f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}")
    monkeypatch.setattr(_github_api, "_renewed", {})
    monkeypatch.setenv("GH_TOKEN", "old")
    minted: list[str] = []

    def mint() -> str:
        minted.append("new")
        return "new"

    monkeypatch.setattr(app_token, "mint_from_env", mint)

    assert _github_api.gh_api_rest("repos/ecmwf/eckit/actions/runs", "old") == {}
    assert _github_api.gh_api_rest("repos/ecmwf/eckit/actions/runs", "old") == {}
    assert log.read_text().split() == ["old", "new", "new"]
    assert minted == ["new"]


def test_a_401_without_credentials_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = 'echo "gh: Bad credentials (HTTP 401)" >&2; exit 1'
    monkeypatch.setenv("PATH", f"{stub_gh(tmp_path, gh)}:{os.environ['PATH']}")
    monkeypatch.setattr(app_token, "mint_from_env", lambda: None)
    assert _github_api.gh_api_rest("repos/ecmwf/eckit/actions/runs", "old") is None
