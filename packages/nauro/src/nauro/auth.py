"""Auth domain logic for remote sync: credentials, token load, token refresh.

This module owns the ``auth`` credential block in ``~/.nauro/config.json`` and
the bearer token read out of it, the shipped Auth0 defaults (domain, client id,
API URL, audience) with their env-var and config-key overrides, the
single-flight refresh-token exchange (module lock, config filelock election,
refuse-to-clobber commit, and a zero-network fast path for a caller whose token
another caller already replaced), and the 429 and 401 retry helpers that
transport callers use.

Library code under ``store/`` and ``sync/`` imports from here. CLI presentation,
the ``nauro auth`` Typer app with its PKCE flow and localhost callback server,
stays in ``nauro.cli.commands.auth``.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable, Mapping

import filelock
import httpx

from nauro.store.config import _config_lock, config_transaction, load_config

# Public OAuth identifiers — safe to ship; not secrets. Do not strip.
DEFAULT_AUTH0_DOMAIN = "dev-q1kuoa1a154u26iw.us.auth0.com"
DEFAULT_AUTH0_CLIENT_ID = "FoVl59QaztJou17Xqr3e2QYOupAr1Ke3"
DEFAULT_API_URL = "https://mcp.nauro.ai"
DEFAULT_AUTH0_AUDIENCE = "https://mcp.nauro.ai/mcp"


class PartialAuthConfigError(Exception):
    """Raised when Auth0 domain/client_id are partially set at a single layer."""


class AuthRefreshError(Exception):
    """Raised when an Auth0 refresh-token exchange fails."""


RATE_LIMITED_MESSAGE = (
    "Auth0 is rate-limiting logins right now. Wait a few seconds and re-run 'nauro auth login'."
)

# Honor Retry-After up to this many seconds. A hostile or buggy header could
# otherwise stall the CLI indefinitely; the ceiling keeps a retry bounded.
_RETRY_AFTER_CEILING_SECONDS = 10.0
# Fixed backoff per attempt when Retry-After is absent or unparseable.
_DEFAULT_BACKOFF_SECONDS = (1.0, 2.0)


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Return the seconds to wait before retrying a 429.

    Only the integer-seconds ``Retry-After`` form is read, clamped to a small ceiling.
    """
    value = response.headers.get("Retry-After")
    if isinstance(value, str):
        seconds = value.strip()
        # ``str.isdigit`` accepts non-ASCII digit characters (e.g. superscript
        # "²") that ``int`` then rejects, so gate on ASCII first.
        if seconds.isascii() and seconds.isdigit():
            return min(float(int(seconds)), _RETRY_AFTER_CEILING_SECONDS)
    index = min(attempt - 1, len(_DEFAULT_BACKOFF_SECONDS) - 1)
    return _DEFAULT_BACKOFF_SECONDS[index]


def post_with_429_retry(
    make_request: Callable[[], httpx.Response], *, max_attempts: int = 3
) -> httpx.Response:
    """Run the zero-arg ``make_request``, retrying with backoff on HTTP 429.

    Returns the final response as-is, a 429 included; never raises, never prints.
    """
    response = make_request()
    for attempt in range(1, max_attempts):
        if response.status_code != 429:
            return response
        time.sleep(_retry_after_seconds(response, attempt))
        response = make_request()
    return response


def resolve_auth_config(
    env: Mapping[str, str], config: Mapping[str, object]
) -> tuple[str, str, str, str]:
    """Resolve ``(domain, client_id, api_url, audience)``: env over config over defaults.
    domain and client_id must come as a pair from one layer, env before config; a partial pair at
    the winning layer raises ``PartialAuthConfigError``; api_url and audience resolve independently.
    """
    env_domain = env.get("NAURO_AUTH0_DOMAIN") or ""
    env_client_id = env.get("NAURO_AUTH0_CLIENT_ID") or ""
    config_domain = str(config.get("auth0_domain") or "")
    config_client_id = str(config.get("auth0_client_id") or "")

    if env_domain and env_client_id:
        domain, client_id = env_domain, env_client_id
    elif env_domain or env_client_id:
        raise PartialAuthConfigError(
            "Partial Auth0 config: NAURO_AUTH0_DOMAIN and NAURO_AUTH0_CLIENT_ID "
            "must be set together."
        )
    elif config_domain and config_client_id:
        domain, client_id = config_domain, config_client_id
    elif config_domain or config_client_id:
        raise PartialAuthConfigError(
            "Partial Auth0 config: auth0_domain and auth0_client_id must be set together in config."
        )
    else:
        domain, client_id = DEFAULT_AUTH0_DOMAIN, DEFAULT_AUTH0_CLIENT_ID

    api_url = env.get("NAURO_API_URL") or str(config.get("api_url") or "") or DEFAULT_API_URL
    audience = (
        env.get("NAURO_AUTH0_AUDIENCE")
        or str(config.get("auth0_audience") or "")
        or DEFAULT_AUTH0_AUDIENCE
    )
    return domain, client_id, api_url, audience


def load_access_token() -> str | None:
    """Return the bearer token written by ``nauro auth login``, or None when no token is stored.
    Callers that need to fail loudly render the "run nauro auth login" guidance themselves.
    """
    auth = load_config().get("auth") or {}
    if not isinstance(auth, dict):
        return None
    token = auth.get("access_token")
    return str(token) if token else None


# Serializes token refresh within a single process: N concurrent 401 callers
# collapse to one /oauth/token exchange while the losers take the zero-network
# fast path. Bounded acquires keep a stuck refresher from blocking others
# forever; on timeout the refresh fails loudly rather than exchanging unguarded,
# which under rotating tokens could trip Auth0 reuse detection and log every
# surface out.
_REFRESH_LOCK = threading.Lock()
_REFRESH_LOCK_TIMEOUT_SECONDS = 30.0
# Bound for the config filelock acquires on the refresh path only. Every other
# config caller keeps the infinite default.
_REFRESH_CONFIG_LOCK_TIMEOUT_SECONDS = 30.0

_REFRESH_TIMEOUT_MESSAGE = (
    "Timed out waiting to refresh credentials. Another refresh may be stuck; "
    "run 'nauro auth login' to re-authenticate."
)


def _exchange_refresh_token(
    domain: str,
    client_id: str,
    refresh_token: str,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, object]:
    """Run the /oauth/token refresh exchange and return ``(new_access_token, rotated_refresh)``.
    ``rotated_refresh`` is whatever Auth0 returned under ``refresh_token``; persisting it is the
    caller's call. Network/429/non-200/non-JSON/missing-token failures raise ``AuthRefreshError``.
    """
    try:
        response = post_with_429_retry(
            lambda: (client.post if client is not None else httpx.post)(
                f"https://{domain}/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                },
                timeout=15.0,
            )
        )
    except httpx.HTTPError as exc:
        raise AuthRefreshError(f"Network error contacting Auth0: {exc}") from exc

    if response.status_code == 429:
        raise AuthRefreshError(RATE_LIMITED_MESSAGE)

    if response.status_code != 200:
        try:
            detail = response.json().get("error_description") or response.text
        except (ValueError, AttributeError):
            detail = response.text
        raise AuthRefreshError(f"Refresh failed ({response.status_code}): {detail}")

    try:
        body = response.json()
    except ValueError as exc:
        raise AuthRefreshError(f"Auth0 returned non-JSON on refresh: {exc}") from exc

    new_access_token = body.get("access_token")
    if not isinstance(new_access_token, str) or not new_access_token:
        raise AuthRefreshError("Auth0 refresh response did not include an access_token.")

    return new_access_token, body.get("refresh_token")


def refresh_access_token(
    stale_access_token: str | None = None, *, client: httpx.Client | None = None
) -> str:
    """Return a fresh access token, persisting it and any rotated refresh token.

    Single-flighted; a failed exchange leaves the stored tokens intact.
    """
    if not _REFRESH_LOCK.acquire(timeout=_REFRESH_LOCK_TIMEOUT_SECONDS):
        raise AuthRefreshError(_REFRESH_TIMEOUT_MESSAGE)
    try:
        # ELECTION: read-only re-validate under a bounded config filelock. If a
        # concurrent refresher already committed, the stored access token now
        # differs from the one that got the 401, so return it with zero network.
        # Otherwise capture the refresh token and release the lock before any
        # network call.
        try:
            with _config_lock(timeout=_REFRESH_CONFIG_LOCK_TIMEOUT_SECONDS):
                config = load_config()
                auth = config.get("auth")
                if not isinstance(auth, dict):
                    auth = {}
                stored_access = auth.get("access_token")
                if (
                    stale_access_token is not None
                    and isinstance(stored_access, str)
                    and stored_access
                    and stored_access != stale_access_token
                ):
                    return stored_access
                refresh_token = auth.get("refresh_token")
                if not refresh_token:
                    raise AuthRefreshError(
                        "No refresh token stored. Run 'nauro auth login' to authenticate."
                    )
        except filelock.Timeout as exc:
            raise AuthRefreshError(_REFRESH_TIMEOUT_MESSAGE) from exc

        try:
            domain, client_id, _api_url, _audience = resolve_auth_config(os.environ, config)
        except PartialAuthConfigError as exc:
            raise AuthRefreshError(str(exc)) from exc

        # EXCHANGE outside the config filelock (the threading lock still
        # serializes us). Holding the filelock across the network call would
        # block every unrelated config writer for the whole retry tail.
        new_access_token, rotated_refresh = _exchange_refresh_token(
            domain, client_id, refresh_token, client=client
        )

        # COMMIT: re-validate under a bounded transaction. If the stored refresh
        # token no longer matches the one we exchanged, a concurrent
        # login/logout/refresh intervened; defer to the stored state rather than
        # clobber it. A fresh stored access token is returned (consistent with
        # the election fast path); if the auth section was cleared by a logout,
        # the refresh fails loudly rather than resurrecting the just-minted token.
        try:
            with config_transaction(timeout=_REFRESH_CONFIG_LOCK_TIMEOUT_SECONDS) as config:
                auth = config.get("auth")
                if not isinstance(auth, dict):
                    auth = {}
                if auth.get("refresh_token") != refresh_token:
                    stored_access = auth.get("access_token")
                    if isinstance(stored_access, str) and stored_access:
                        return stored_access
                    raise AuthRefreshError(
                        "Authentication was cleared during refresh. "
                        "Run 'nauro auth login' to authenticate."
                    )
                auth["access_token"] = new_access_token
                if isinstance(rotated_refresh, str) and rotated_refresh:
                    auth["refresh_token"] = rotated_refresh
                config["auth"] = auth
        except filelock.Timeout as exc:
            raise AuthRefreshError(_REFRESH_TIMEOUT_MESSAGE) from exc

        return new_access_token
    finally:
        _REFRESH_LOCK.release()


def with_token_refresh(
    call: Callable[[str], httpx.Response], *, client: httpx.Client | None = None
) -> httpx.Response:
    """Run ``call(access_token)``, refreshing once and retrying on a first 401.

    A second 401 is returned to the caller; a failed refresh raises ``AuthRefreshError``.
    """
    token = load_access_token()
    if token is None:
        raise AuthRefreshError("Not authenticated. Run 'nauro auth login' to authenticate.")

    response = call(token)
    if response.status_code != 401:
        return response

    new_token = refresh_access_token(stale_access_token=token, client=client)
    return call(new_token)


def decode_jwt_payload(token: str) -> dict:
    """Base64-decode the JWT payload (no cryptographic verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload_b64 = parts[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)
