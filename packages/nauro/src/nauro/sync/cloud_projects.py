"""HTTP client for the remote MCP server's project endpoints.

Wraps ``POST /projects`` and ``GET /projects`` on the Nauro cloud control
plane. Both endpoints require an OAuth bearer token; auth is shared with
the sync transport via ``with_token_refresh`` — a stale access token is
refreshed transparently on 401 (when a refresh token is available), so a
session that has only been idle still completes ``nauro init --cloud``
and ``nauro attach`` without an interactive re-login.

Server URL resolution is shared with the sync transport via
``nauro.sync.remote.resolve_api_url`` (``NAURO_API_URL`` env var, then
``api_url`` in user config, then the public default).

Failure modes are collapsed into a single ``CloudProjectError`` with a
human-readable message; both callers just render the message. The 4xx
auth branches distinguish ``401 after refresh attempt`` from ``403
forbidden`` so the remediation hint matches the underlying cause instead
of always telling the user to log in.

``created_at`` is passed through verbatim as an ISO 8601 string. No date
parsing — that just creates timezone-normalization fragility for no caller
benefit.
"""

from __future__ import annotations

from typing import TypedDict

import httpx

from nauro.cli.commands.auth import AuthRefreshError, with_token_refresh
from nauro.sync.remote import resolve_api_url

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


def _parse_project(raw: object) -> ProjectView:
    """Coerce a server-returned object into a ProjectView."""
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
    """Issue an authenticated request, refreshing the token on 401.

    Auth handling is delegated to :func:`with_token_refresh`: a fresh
    request runs first, and a 401 triggers one refresh + retry before the
    response surfaces here. Transport errors, refresh failures, and
    persistent 4xx/5xx responses are all translated into
    :class:`CloudProjectError` with a remediation hint matched to the
    actual failure mode.
    """
    url = f"{resolve_api_url()}{path}"

    def _call(token: str) -> httpx.Response:
        return httpx.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            json=json_body,
            timeout=_DEFAULT_TIMEOUT,
        )

    try:
        response = with_token_refresh(_call)
    except AuthRefreshError as exc:
        # ``AuthRefreshError`` messages already carry the right remediation
        # for the case they describe (no refresh token / expired refresh /
        # network failure to Auth0); appending another "run nauro auth
        # login" line just makes them double-sentence and unclear.
        raise CloudProjectError(f"Authentication failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise CloudProjectError(f"Network error contacting {url}: {exc}") from exc

    if response.status_code == 401:
        # Reached only after with_token_refresh already attempted a refresh
        # and retry; a second 401 means the refreshed token was rejected by
        # the server (revoked, server-side identity change, or maintenance).
        raise CloudProjectError(
            "Authentication rejected (401) even after refreshing the token. "
            "Run 'nauro auth login' to re-authenticate."
        )
    if response.status_code == 403:
        raise CloudProjectError(
            f"Forbidden (403) from {url}. Your account does not have access to this resource."
        )
    if response.status_code >= 500:
        raise CloudProjectError(
            f"Server error ({response.status_code}) from {url}. "
            "The cloud control plane may be unavailable; try again shortly."
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

    items = payload["projects"] if isinstance(payload, dict) and "projects" in payload else payload

    if not isinstance(items, list):
        raise CloudProjectError(f"Unexpected /projects response shape (not a list): {items!r}")
    return [_parse_project(item) for item in items]
