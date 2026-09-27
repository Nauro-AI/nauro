"""Read-only discovery of a hosted project's write authority for the connection flow.

One ``GET /projects?project_id=`` with the legacy access token, against the origin
resolved from the environment and the global config. Only a positive owner answer
under generation authority reads as ``generation``; every refusal, error, missing
credential or unexpected shape reads as ``legacy``. It never touches the generation
credential store and writes nothing outside the existing legacy token refresh and
the existing corrupt-config quarantine. The result is advisory: any generation step
re-verifies owner access with its own credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from nauro.auth import AuthRefreshError, with_token_refresh
from nauro.sync.cloud_projects import _DEFAULT_TIMEOUT
from nauro.sync.remote import resolve_api_url


@dataclass(frozen=True)
class AuthorityDiscovery:
    """Discovered authority plus the observed status and server detail, for diagnostics."""

    authority: Literal["generation", "legacy"]
    status: int | None
    detail: str | None


def discover_project_authority(project_id: str) -> AuthorityDiscovery:
    """Return ``generation`` only for a 200 generation-owner answer naming this project."""
    if not isinstance(project_id, str):
        raise TypeError("project_id must be a string")

    try:
        url = resolve_api_url() + "/projects"

        def call(token: str) -> httpx.Response:
            return httpx.request(
                "GET",
                url,
                params={"project_id": project_id},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=_DEFAULT_TIMEOUT,
            )

        response = with_token_refresh(call)
    except (
        AuthRefreshError,
        httpx.HTTPError,
        httpx.InvalidURL,
        OSError,
        ValueError,
        RecursionError,
    ):
        return AuthorityDiscovery("legacy", None, None)
    try:
        body = response.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return AuthorityDiscovery("legacy", response.status_code, None)
    detail = body.get("detail")
    projects = body.get("projects")
    owner = (
        response.status_code == 200
        and body.get("authority") == "generation_owner"
        and isinstance(projects, list)
        and any(
            isinstance(item, dict)
            and item.get("project_id") == project_id
            and item.get("role") == "owner"
            for item in projects
        )
    )
    return AuthorityDiscovery(
        "generation" if owner else "legacy",
        response.status_code,
        detail if isinstance(detail, str) else None,
    )
