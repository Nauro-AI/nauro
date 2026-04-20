"""nauro auth — Auth0 Authorization Code + PKCE for remote MCP sync.

Uses a localhost redirect with a temporary HTTP server, the standard
pattern for CLI tools (gh auth login, gcloud auth login). Tokens are
stored in ~/.nauro/config.json under the "auth" key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import webbrowser
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import typer

from nauro.store.config import load_config, save_config

logger = logging.getLogger("nauro.auth")

auth_app = typer.Typer(help="Manage authentication for remote sync.")

# Public OAuth identifiers — safe to ship; not secrets. Do not strip.
DEFAULT_AUTH0_DOMAIN = "dev-q1kuoa1a154u26iw.us.auth0.com"
DEFAULT_AUTH0_CLIENT_ID = "FoVl59QaztJou17Xqr3e2QYOupAr1Ke3"
DEFAULT_API_URL = "https://mcp.nauro.ai"
DEFAULT_AUTH0_AUDIENCE = "https://mcp.nauro.ai/mcp"
AUTH0_SCOPES = "openid profile offline_access read:context write:context"
REDIRECT_PORT = 18457
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


class PartialAuthConfigError(Exception):
    """Raised when Auth0 domain/client_id are partially set at a single layer."""


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
    env_d = env.get("NAURO_AUTH0_DOMAIN") or ""
    env_c = env.get("NAURO_AUTH0_CLIENT_ID") or ""
    cfg_d = str(config.get("auth0_domain") or "")
    cfg_c = str(config.get("auth0_client_id") or "")

    if env_d and env_c:
        domain, client_id = env_d, env_c
    elif env_d or env_c:
        raise PartialAuthConfigError(
            "Partial Auth0 config: NAURO_AUTH0_DOMAIN and NAURO_AUTH0_CLIENT_ID "
            "must be set together."
        )
    elif cfg_d and cfg_c:
        domain, client_id = cfg_d, cfg_c
    elif cfg_d or cfg_c:
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


def _sanitize_sub(sub: str) -> str:
    """Sanitize an Auth0 ``sub`` claim for use in S3 key paths.

    Must match the server-side logic in ``mcp-server/src/mcp_server/app.py``.
    """
    safe = re.sub(r"[^a-zA-Z0-9_\\-]", "-", sub)
    return safe[:128]


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
        html = f"<html><body><h2>{message}</h2></body></html>"
        self.wfile.write(html.encode())

    def log_message(self, format, *args):  # noqa: A002
        """Suppress default stderr logging."""
        pass


@auth_app.command()
def login() -> None:
    """Authenticate with Auth0 using Authorization Code + PKCE."""
    try:
        domain, client_id, api_url, audience = _resolve_auth_config(os.environ, load_config())
    except PartialAuthConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)

    # Reset handler state
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None

    # Start local server to receive callback
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    # Build authorization URL
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

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    typer.echo("Waiting for authorization...")

    # Wait for callback (timeout after 120 seconds)
    server_thread.join(timeout=120)
    server.server_close()

    if _CallbackHandler.error:
        typer.echo(f"Authorization failed: {_CallbackHandler.error}", err=True)
        raise typer.Exit(code=1)

    if not _CallbackHandler.auth_code:
        typer.echo("Authorization timed out. Please try again.", err=True)
        raise typer.Exit(code=1)

    # Exchange code for tokens
    try:
        token_resp = httpx.post(
            f"https://{domain}/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": _CallbackHandler.auth_code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
        token_resp.raise_for_status()
    except httpx.HTTPError as exc:
        typer.echo(f"Token exchange failed: {exc}", err=True)
        raise typer.Exit(code=1)

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
        raise typer.Exit(code=1)

    sanitized_sub = _sanitize_sub(sub)

    # Fetch canonical user_id from server
    user_id = None
    try:
        me_resp = httpx.get(
            f"{api_url}/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_resp.raise_for_status()
        user_id = me_resp.json().get("user_id")
    except Exception as e:
        logger.warning("Failed to fetch user_id from /me: %s", e)
        typer.echo(
            "  Warning: could not fetch user_id from server."
            " Sync will use sanitized_sub as fallback.",
            err=True,
        )

    # Persist to config
    config = load_config()
    config["auth"] = {
        "sub": sub,
        "sanitized_sub": sanitized_sub,
        "user_id": user_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    save_config(config)

    typer.echo(f"Authenticated as {sub}")


@auth_app.command()
def status() -> None:
    """Show current authentication state."""
    config = load_config()
    auth = config.get("auth")

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

    del config["auth"]
    save_config(config)
    typer.echo("Logged out. Auth credentials removed from config.")
