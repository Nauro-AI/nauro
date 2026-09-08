"""Synthetic normal credentials for replica integration tests."""

import time

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI
from nauro.store.config import save_config
from nauro.sync.generation_connection import connection_for
from nauro.sync.generation_credentials import AccountRecord


def seed_generation_account(binding, actor, monkeypatch):
    for key in (
        "NAURO_AUTH0_DOMAIN",
        "NAURO_AUTH0_CLIENT_ID",
        "NAURO_API_URL",
        "NAURO_AUTH0_AUDIENCE",
    ):
        monkeypatch.delenv(key, raising=False)
    save_config(
        {
            "api_url": binding.server_url,
            "auth0_domain": "issuer.example",
            "auth0_client_id": "client",
            "auth0_audience": binding.server_url + "/mcp",
        }
    )
    connection = connection_for(binding, DEFAULT_AUTH_REDIRECT_URI)
    account = connection.store()
    with account.locked():
        account.write(
            AccountRecord(
                revision="initial",
                binding=account.binding,
                state="active",
                user_id=actor,
                subject="synthetic-owner",
                access_token="normal-generation-token",
                refresh_token="synthetic-refresh",
                expires_at=int(time.time()) + 600,
            )
        )
    return connection
