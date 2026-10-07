"""Signed synthetic provider for automatic-renewal surface tests."""

import time

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from nauro.sync import generation_renewal as renewal


def install_provider(monkeypatch, connection):
    calls = []
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "synthetic-renewal"

    def wire(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            record = connection.store().read()
            token = jwt.encode(
                {
                    "iss": connection.issuer,
                    "aud": connection.audience,
                    "azp": connection.client_id,
                    "sub": record.subject,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 600,
                    "scope": "read:context write:context",
                },
                key,
                algorithm="RS256",
                headers={"kid": jwk["kid"]},
            )
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})
        assert request.url.path == "/.well-known/jwks.json"
        return httpx.Response(200, json={"keys": [jwk]})

    def run(request, timeout):
        with httpx.Client(transport=httpx.MockTransport(wire)) as client:
            renewal.renew_requested_credentials(request, client)

    monkeypatch.setattr(renewal, "_run_worker", run)
    return calls


def expire(connection):
    store = connection.store()
    with store.locked():
        store.write(store.read().model_copy(update={"expires_at": int(time.time()) - 1}))
