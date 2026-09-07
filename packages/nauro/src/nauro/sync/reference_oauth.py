"""Pinned OAuth exchanges for explicit reference authentication."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from nauro.sync.decision_profile import RenewalProfile
from nauro.sync.decision_reference_contract import _json


def response_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> Any:
    with client.stream(method, url, timeout=15, follow_redirects=False, **kwargs) as response:
        if response.status_code != 200:
            raise ValueError("Authentication request failed")
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 65536:
                raise ValueError("Authentication response exceeds limit")
        value = _json(bytes(data))
        if not isinstance(value, dict):
            raise ValueError("Authentication response must be an object")
        return value


def verify_access(profile: RenewalProfile, token: str, client: httpx.Client) -> int:
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("Unsupported token signing key")
    keys = response_json(client, "GET", profile.issuer + ".well-known/jwks.json")
    if not isinstance(keys.get("keys"), list) or not all(
        isinstance(key, dict) for key in keys["keys"]
    ):
        raise ValueError("Invalid signing key set")
    candidates = [key for key in keys["keys"] if key.get("kid") == header["kid"]]
    if len(candidates) != 1:
        raise ValueError("Ambiguous token signing key")
    candidate = candidates[0]
    if (
        candidate.get("kty") != "RSA"
        or candidate.get("use", "sig") != "sig"
        or candidate.get("alg", "RS256") != "RS256"
    ):
        raise ValueError("Unsupported signing key")
    key = jwt.PyJWK.from_dict(candidate, algorithm="RS256")
    claims = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        issuer=profile.issuer,
        audience=profile.audience,
        options={"require": ["exp", "iat", "iss", "aud", "sub", "azp", "scope"]},
    )
    if claims["sub"] != profile.expected_subject or claims["azp"] != profile.client_id:
        raise ValueError("Token identity differs from profile")
    scope = claims["scope"]
    if not isinstance(scope, str) or not {"read:context", "write:context"} <= set(scope.split()):
        raise ValueError("Required token scopes missing")
    if type(claims["exp"]) is not int:
        raise ValueError("Invalid token expiry")
    return int(claims["exp"])


def exchange(
    profile: RenewalProfile, client: httpx.Client, grant: dict[str, str]
) -> tuple[str, str, int]:
    body = response_json(
        client,
        "POST",
        profile.issuer + "oauth/token",
        json={"client_id": profile.client_id, **grant},
    )
    access, refresh = body.get("access_token"), body.get("refresh_token")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or not isinstance(body.get("token_type"), str)
        or body["token_type"].lower() != "bearer"
    ):
        raise ValueError("Incomplete rotating credentials")
    return access, refresh, verify_access(profile, access, client)


def callback_code(
    profile: RenewalProfile,
    present_url: Callable[[str], None],
    timeout: float = 120,
) -> tuple[str, str]:
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    result: list[str | None] = []

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            valid = (
                url.path == "/callback"
                and params.get("state") == [state]
                and len(params.get("code", [])) == 1
                and "error" not in params
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to the terminal.")
            if valid:
                result.append(params["code"][0])
            elif params.get("state") == [state] and "error" in params:
                result.append(None)

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def setup(self) -> None:
            self.request.settimeout(1)
            super().setup()

    port = urlsplit(profile.redirect_uri).port
    assert port is not None
    with HTTPServer(("127.0.0.1", port), Callback) as server:
        server.timeout = 0.2
        present_url(
            profile.issuer
            + "authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": profile.client_id,
                    "redirect_uri": profile.redirect_uri,
                    "audience": profile.audience,
                    "scope": "openid offline_access read:context write:context",
                    "state": state,
                    "code_challenge": challenge.decode(),
                    "code_challenge_method": "S256",
                }
            )
        )
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result or result[0] is None:
        raise ValueError("Reference login refused or timed out")
    return result[0], verifier
