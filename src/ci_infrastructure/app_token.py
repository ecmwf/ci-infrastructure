# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Mint a GitHub App installation token in the middle of a step.

A token from create-github-app-token expires after an hour, but a wait for upstream builds can
take up to ``ARTIFACT_WAIT_TIMEOUT``. Given the App's credentials, the step mints a new one.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
from typing import Any, Final

CLIENT_ID_ENV: Final = "CI_APP_CLIENT_ID"
PRIVATE_KEY_ENV: Final = "CI_APP_PRIVATE_KEY"
API_URL: Final = "https://api.github.com"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(client_id: str, private_key_pem: str, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(time.time() if now is None else now) - 60  # GitHub's advice against clock drift
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iat": issued, "exp": issued + 600, "iss": client_id}).encode())
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("the App private key is not an RSA key")
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def _call(method: str, path: str, jwt: str) -> Any:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def mint(client_id: str, private_key_pem: str, owner: str) -> str:
    """A token of the App's installation on ``owner``, for all the repositories it is installed on."""
    jwt = app_jwt(client_id, private_key_pem)
    installation = _call("GET", f"/orgs/{owner}/installation", jwt)
    return str(_call("POST", f"/app/installations/{installation['id']}/access_tokens", jwt)["token"])


def mint_from_env() -> str | None:
    """A new token from the App credentials in the env, or None without them or on failure."""
    client_id = os.environ.get(CLIENT_ID_ENV)
    private_key = os.environ.get(PRIVATE_KEY_ENV)
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    if not (client_id and private_key and owner):
        return None
    try:
        return mint(client_id, private_key, owner)
    except (OSError, ValueError, KeyError) as err:
        print(f"::warning::Could not mint a new App token: {err}", file=sys.stderr)
        return None
