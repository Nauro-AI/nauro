"""HTTP client for the remote MCP server's project endpoints.

Wraps ``POST /projects`` and ``GET /projects`` on the Nauro cloud control
plane. Both endpoints require an OAuth bearer token; the token is loaded
from ``~/.nauro/config.json`` under the ``auth.access_token`` key — the same
slot that ``nauro auth login`` writes.

The server URL resolution mirrors ``nauro.cli.commands.auth``: ``NAURO_API_URL``
env var first, then ``api_url`` in user config, then the public default.

Failure modes are collapsed into a single ``CloudProjectError`` with a
human-readable message; both callers (``nauro init --cloud`` and ``nauro attach``,
landing in 2c-B) just render the message.

``created_at`` is passed through verbatim as an ISO 8601 string. No date
parsing — that just creates timezone-normalization fragility for no caller
benefit.
"""

from __future__ import annotations

import os
from typing import TypedDict

import httpx

from nauro.cli.commands.auth import DEFAULT_API_URL
from nauro.store.config import load_config

_DEFAULT_TIMEOUT = 15.0


class ProjectView(TypedDict):
    """Server-side project descriptor returned by /projects endpoints."""

    project_id: str
    name: str
    role: str
    created_at: str


class CloudProjectError(Exception):
    """Raised for any failure invoking the cloud project endpoints.

    The message is the user-facing rendering. Callers should print it and
    exit; no recovery is attempted at this layer.
    """


def _resolve_api_url() -> str:
    """Resolve the remote MCP server base URL.

    Precedence: NAURO_API_URL env var → ``api_url`` config key → public default.
    Mirrors the resolution in ``nauro.cli.commands.auth._resolve_auth_config``
    so this client and the auth flow agree about which server is in play.
    """
    env_url = os.environ.get("NAURO_API_URL")
    if env_url:
        return env_url
    config_url = str(load_config().get("api_url") or "")
    return config_url or DEFAULT_API_URL


def _load_access_token() -> str:
    """Read the OAuth bearer token written by ``nauro auth login``.

    Raises:
        CloudProjectError: If no token is available.
    """
    auth = load_config().get("auth") or {}
    token = auth.get("access_token") if isinstance(auth, dict) else None
    if not token:
        raise CloudProjectError(
            "Not authenticated. Run 'nauro auth login' before targeting the cloud."
        )
    return str(token)


def _parse_project(raw: object) -> ProjectView:
    """Coerce a server-returned object into a ProjectView.

    Pass-through ISO 8601 string for ``created_at`` — no date parsing.
    """
    if not isinstance(raw, dict):
        raise CloudProjectError(f"Unexpected project payload from server (not an object): {raw!r}")
    try:
        return ProjectView(
            project_id=str(raw["project_id"]),
            name=str(raw["name"]),
            role=str(raw["role"]),
            created_at=str(raw["created_at"]),
        )
    except KeyError as exc:
        raise CloudProjectError(
            f"Project payload from server is missing required field: {exc.args[0]}"
        ) from exc


def _request(method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
    """Issue an authenticated request, translating transport failures."""
    api_url = _resolve_api_url().rstrip("/")
    url = f"{api_url}{path}"
    headers = {
        "Authorization": f"Bearer {_load_access_token()}",
        "Accept": "application/json",
    }
    try:
        response = httpx.request(
            method, url, headers=headers, json=json_body, timeout=_DEFAULT_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise CloudProjectError(f"Network error contacting {url}: {exc}") from exc

    if response.status_code in (401, 403):
        raise CloudProjectError(
            f"Authentication failed ({response.status_code}). Run 'nauro auth login' and try again."
        )
    if response.status_code >= 500:
        raise CloudProjectError(
            f"Server error ({response.status_code}) from {url}. "
            f"The cloud control plane may be unavailable; try again shortly."
        )
    if response.status_code >= 400:
        # 4xx other than auth — surface server message verbatim where possible.
        try:
            detail = response.json().get("detail") or response.text
        except (ValueError, AttributeError):
            detail = response.text
        raise CloudProjectError(f"Request failed ({response.status_code}) from {url}: {detail}")
    return response


def create_project(name: str) -> ProjectView:
    """Create a new cloud-scoped project.

    Args:
        name: Human-readable project name. Server is the source of truth for
            naming rules; this client passes the value through unchanged.

    Returns:
        ProjectView for the freshly minted project, including the
        server-assigned ``project_id`` (ULID).
    """
    response = _request("POST", "/projects", json_body={"name": name})
    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudProjectError(
            f"Server returned non-JSON for POST /projects: {response.text!r}"
        ) from exc
    return _parse_project(payload)


def list_projects() -> list[ProjectView]:
    """List every cloud-scoped project visible to the current OAuth identity.

    Returns:
        Project list in server order. May be empty.
    """
    response = _request("GET", "/projects")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudProjectError(
            f"Server returned non-JSON for GET /projects: {response.text!r}"
        ) from exc

    if isinstance(payload, dict) and "projects" in payload:
        items = payload["projects"]
    else:
        items = payload

    if not isinstance(items, list):
        raise CloudProjectError(f"Unexpected /projects response shape (not a list): {items!r}")
    return [_parse_project(item) for item in items]
