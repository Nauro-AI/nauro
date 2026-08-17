"""nauro auth — Auth0 Authorization Code + PKCE for remote MCP sync.

Uses a localhost redirect with a temporary HTTP server, the standard
pattern for CLI tools (gh auth login, gcloud auth login). Tokens are
stored in ~/.nauro/config.json under the "auth" key.

Only the CLI surface lives here: the Typer app, the PKCE material, the
loopback callback server, and the login/status/logout commands. The auth
domain logic they call — shipped Auth0 defaults, config resolution, token
load, refresh, and the retry helpers — lives in ``nauro.auth``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import logging
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import typer
from nauro_core import sanitize_sub

from nauro.auth import (
    RATE_LIMITED_MESSAGE,
    PartialAuthConfigError,
    decode_jwt_payload,
    post_with_429_retry,
    resolve_auth_config,
)
from nauro.store.config import config_transaction, load_config

logger = logging.getLogger("nauro.auth")

auth_app = typer.Typer(help="Manage authentication for remote sync.")

AUTH0_SCOPES = "openid profile email offline_access read:context write:context"
REDIRECT_PORT = 18457
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


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
        domain, client_id, api_url, audience = resolve_auth_config(os.environ, load_config())
    except PartialAuthConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    auth_code, code_verifier = _run_callback_flow(domain, client_id, audience)

    # Exchange code for tokens
    try:
        token_resp = post_with_429_retry(
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
        payload = decode_jwt_payload(access_token)
        sub = payload["sub"]
    except (ValueError, KeyError) as exc:
        typer.echo(f"Failed to decode access token: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    sanitized_sub = sanitize_sub(sub)

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
    with config_transaction() as config:
        config["auth"] = {
            "sub": sub,
            "sanitized_sub": sanitized_sub,
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
        }

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
        typer.echo("Not authenticated - nothing to clear.")
        return

    with config_transaction() as config:
        config.pop("auth", None)
    typer.echo("Logged out. Auth credentials removed from config.")
