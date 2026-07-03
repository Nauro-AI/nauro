"""nauro auth — Auth0 Authorization Code + PKCE for remote MCP sync.

Uses a localhost redirect with a temporary HTTP server, the standard
pattern for CLI tools (gh auth login, gcloud auth login). Tokens are
stored in ~/.nauro/config.json under the "auth" key.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import filelock
import httpx
import typer
from nauro_core import sanitize_sub

from nauro.store.config import _config_lock, config_transaction, load_config

logger = logging.getLogger("nauro.auth")

auth_app = typer.Typer(help="Manage authentication for remote sync.")

# Public OAuth identifiers — safe to ship; not secrets. Do not strip.
DEFAULT_AUTH0_DOMAIN = "dev-q1kuoa1a154u26iw.us.auth0.com"
DEFAULT_AUTH0_CLIENT_ID = "FoVl59QaztJou17Xqr3e2QYOupAr1Ke3"
DEFAULT_API_URL = "https://mcp.nauro.ai"
DEFAULT_AUTH0_AUDIENCE = "https://mcp.nauro.ai/mcp"
AUTH0_SCOPES = "openid profile email offline_access read:context write:context"
REDIRECT_PORT = 18457
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


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
    """Pick a backoff duration for a 429 retry.

    Reads the integer-seconds form of the ``Retry-After`` header using plain
    string operations and clamps it to a small ceiling. The HTTP-date form is
    ignored. When the header is absent or unparseable, falls back to a short
    per-attempt default.
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


def _post_with_429_retry(
    make_request: Callable[[], httpx.Response], *, max_attempts: int = 3
) -> httpx.Response:
    """Run ``make_request`` and retry with backoff on HTTP 429.

    ``make_request`` is a zero-arg callable returning an ``httpx.Response``;
    each caller closes over its own URL, body, and timeout. Any non-429
    response is returned immediately. On a 429 with attempts remaining, sleep
    for the backoff interval and retry. A 429 on the final attempt is returned
    as-is so the caller can render its own guidance — this helper never raises
    and never emits user-facing messaging.
    """
    response = make_request()
    for attempt in range(1, max_attempts):
        if response.status_code != 429:
            return response
        time.sleep(_retry_after_seconds(response, attempt))
        response = make_request()
    return response


def _resolve_auth_config(
    env: Mapping[str, str], config: Mapping[str, object]
) -> tuple[str, str, str, str]:
    """Resolve (domain, client_id, api_url, audience).

    (domain, client_id) must come from the same source — mixing tenants produces
    confusing Auth0 errors. A partial env pair (one of the two set without the
    other) raises rather than falling through to config or defaults; a stale
    shell export is usually the cause and silent fall-through would hide it.
    api_url and audience resolve independently.
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
    """Read the OAuth bearer token written by ``nauro auth login``.

    Returns ``None`` when no token is present. Callers that need to fail loudly
    should render the "run nauro auth login" guidance themselves.
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


def _exchange_refresh_token(domain: str, client_id: str, refresh_token: str) -> tuple[str, object]:
    """Run the /oauth/token refresh exchange.

    Returns ``(new_access_token, rotated_refresh)`` where ``rotated_refresh`` is
    whatever Auth0 returned under ``refresh_token`` (a string when the client
    rotates, otherwise absent); the caller decides whether to persist it. Every
    failure mode raises ``AuthRefreshError``.
    """
    try:
        response = _post_with_429_retry(
            lambda: httpx.post(
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


def refresh_access_token(stale_access_token: str | None = None) -> str:
    """Exchange the stored refresh token for a fresh access token.

    Single-flighted within the process by a module-level lock: one caller runs
    the /oauth/token exchange while concurrent callers wait and then take a
    zero-network fast path. Persists the new access token (and the new refresh
    token, if Auth0 rotates it). Stored tokens are left intact on failure so the
    user can retry without losing state.

    ``stale_access_token`` is the token that just received a 401. When another
    caller has already refreshed, the stored token now differs from it and the
    fast path returns that stored token with no network call. Direct callers
    that pass ``None`` always run the exchange.
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
            domain, client_id, _api_url, _audience = _resolve_auth_config(os.environ, config)
        except PartialAuthConfigError as exc:
            raise AuthRefreshError(str(exc)) from exc

        # EXCHANGE outside the config filelock (the threading lock still
        # serializes us). Holding the filelock across the network call would
        # block every unrelated config writer for the whole retry tail.
        new_access_token, rotated_refresh = _exchange_refresh_token(
            domain, client_id, refresh_token
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


def with_token_refresh(call: Callable[[str], httpx.Response]) -> httpx.Response:
    """Run ``call(access_token)`` and refresh once on 401.

    The first 401 triggers a refresh and a single retry. A second 401 (or any
    other status) is returned to the caller — there is no infinite loop. A
    failed refresh propagates as ``AuthRefreshError`` so the caller can guide
    the user to ``nauro auth login``.
    """
    token = load_access_token()
    if token is None:
        raise AuthRefreshError("Not authenticated. Run 'nauro auth login' to authenticate.")

    response = call(token)
    if response.status_code != 401:
        return response

    new_token = refresh_access_token(stale_access_token=token)
    return call(new_token)


def _decode_jwt_payload(token: str) -> dict:
    """Base64-decode the JWT payload (no cryptographic verification)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    payload_b64 = parts[1]
    # Add padding if needed
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _callback_page(message: str) -> str:
    """Render the loopback callback page, escaping the message.

    ``message`` can carry an Auth0-supplied ``error_description`` reflected from
    the redirect query, so it is HTML-escaped to keep an attacker-influenced
    error string from injecting markup into the page the browser renders.
    """
    return f"<html><body><h2>{html.escape(message)}</h2></body></html>"


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth callback code."""

    auth_code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self._respond("Login successful! You can close this tab and return to the terminal.")
        elif "error" in params:
            desc = params.get("error_description", params.get("error", ["Unknown error"]))
            _CallbackHandler.error = desc[0] if isinstance(desc, list) else desc
            self._respond(f"Login failed: {_CallbackHandler.error}")
        else:
            self._respond("Unexpected callback. Please try again.")

    def _respond(self, message: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_callback_page(message).encode())

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


def _run_callback_flow(domain: str, client_id: str, audience: str) -> tuple[str, str]:
    """Drive the browser-based Auth0 callback flow and return ``(auth_code, code_verifier)``.

    Generates PKCE material, starts a localhost server to receive the redirect,
    opens the browser, and waits up to 120 seconds for Auth0 to deliver an
    authorization code. The local server is always closed before returning,
    even on the timeout/error paths.
    """
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    try:
        auth_params = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": AUTH0_SCOPES,
                "audience": audience,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
            }
        )
        auth_url = f"https://{domain}/authorize?{auth_params}"

        typer.echo("\nOpening browser to authenticate...\n")
        typer.echo(f"If the browser doesn't open, visit:\n  {auth_url}\n")

        with contextlib.suppress(Exception):
            webbrowser.open(auth_url)

        typer.echo("Waiting for authorization...")
        server_thread.join(timeout=120)
    finally:
        server.server_close()

    if _CallbackHandler.error:
        typer.echo(f"Authorization failed: {_CallbackHandler.error}", err=True)
        raise typer.Exit(code=1)

    if not _CallbackHandler.auth_code:
        typer.echo("Authorization timed out. Please try again.", err=True)
        raise typer.Exit(code=1)

    return _CallbackHandler.auth_code, code_verifier


@auth_app.command()
def login() -> None:
    """Authenticate with Auth0 using Authorization Code + PKCE."""
    try:
        domain, client_id, api_url, audience = _resolve_auth_config(os.environ, load_config())
    except PartialAuthConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    auth_code, code_verifier = _run_callback_flow(domain, client_id, audience)

    # Exchange code for tokens
    try:
        token_resp = _post_with_429_retry(
            lambda: httpx.post(
                f"https://{domain}/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": auth_code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": code_verifier,
                },
                timeout=15.0,
            )
        )
        if token_resp.status_code == 429:
            typer.echo(RATE_LIMITED_MESSAGE, err=True)
            raise typer.Exit(code=1)
        token_resp.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo(f"Token exchange failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    body = token_resp.json()

    if "error" in body:
        err_msg = body.get("error_description", body["error"])
        typer.echo(f"Token exchange failed: {err_msg}", err=True)
        raise typer.Exit(code=1)

    access_token = body["access_token"]
    refresh_token = body.get("refresh_token")

    # Decode JWT to get sub
    try:
        payload = _decode_jwt_payload(access_token)
        sub = payload["sub"]
    except (ValueError, KeyError) as exc:
        typer.echo(f"Failed to decode access token: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    sanitized_sub = sanitize_sub(sub)

    # Fetch canonical user_id from server
    user_id = None
    me_body: dict[str, object] = {}
    try:
        me_resp = httpx.get(
            f"{api_url}/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_resp.raise_for_status()
        me_body = me_resp.json()
        user_id = me_body.get("user_id")
    except Exception as e:
        logger.warning("Failed to fetch user_id from /me: %s", e)
        typer.echo(
            "  Warning: could not fetch user_id from server."
            " Sync will use sanitized_sub as fallback.",
            err=True,
        )

    # Persist to config
    with config_transaction() as config:
        config["auth"] = {
            "sub": sub,
            "sanitized_sub": sanitized_sub,
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

    # Telemetry identity merge: auth state is already persisted above —
    # this block only handles the PostHog alias+set. Raw email never leaves
    # auth.py; identify_login receives only the SHA-256 hex digest.
    try:
        email_raw = (
            payload.get("email")
            or payload.get("https://mcp.nauro.ai/email")
            or me_body.get("email")
            or ""
        )
        email = email_raw.strip().lower() if isinstance(email_raw, str) else ""
        if user_id and email:
            from nauro.telemetry import identify_login as _telemetry_identify_login

            email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
            _telemetry_identify_login(user_id=user_id, email_hash=email_hash)
    except Exception:
        logger.debug("telemetry identify_login failed", exc_info=True)

    typer.echo(f"Authenticated as {sub}")

    typer.echo(
        "\nNext steps:\n"
        "  To promote a local project and sync it:\n"
        "    nauro link --cloud    (one-time, per project)\n"
        "    nauro sync\n"
        "\n"
        "  Add https://mcp.nauro.ai/mcp as an MCP connector in your tool's settings\n"
        "  (enter the URL exactly, with no trailing slash).\n"
        "\n"
        "  Codex: add `mcp_oauth_callback_port = 8765` to the top of ~/.codex/config.toml\n"
        "  (required for the remote connector; without it, login uses a random port"
        " and fails)."
    )


@auth_app.command()
def status() -> None:
    """Show current authentication state."""
    config = load_config()
    auth = config.get("auth")
    if not isinstance(auth, dict):
        auth = {}

    if not auth or not auth.get("access_token"):
        typer.echo("Not authenticated. Run 'nauro auth login' to sign in.")
        raise typer.Exit(code=1)

    sub = auth.get("sub", "(unknown)")
    sanitized = auth.get("sanitized_sub", "(unknown)")
    user_id = auth.get("user_id", "(not set)")
    has_refresh = "yes" if auth.get("refresh_token") else "no"

    typer.echo(f"Authenticated as: {sub}")
    typer.echo(f"User ID:          {user_id}")
    typer.echo(f"Sanitized sub:    {sanitized}")
    typer.echo(f"Refresh token:    {has_refresh}")


@auth_app.command()
def logout() -> None:
    """Clear stored authentication credentials."""
    config = load_config()
    if "auth" not in config:
        typer.echo("Not authenticated — nothing to clear.")
        return

    # Two sequential standalone transactions, never nested: the config lock is
    # not re-entrant, so identify_logout (which opens its own rotation
    # transaction) must run outside the auth-removal transaction. The two writes
    # touch disjoint keys (telemetry.anonymous_id vs auth), so ordering is
    # correctness-neutral; rotate first to preserve the documented
    # "auth cleared after rotation" sequence.
    try:
        from nauro.telemetry import identify_logout as _telemetry_identify_logout

        _telemetry_identify_logout()
    except Exception:
        logger.debug("telemetry identify_logout failed", exc_info=True)

    with config_transaction() as config:
        config.pop("auth", None)
    typer.echo("Logged out. Auth credentials removed from config.")
