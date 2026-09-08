"""Select normal authentication from the current project's authority."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from nauro.auth import PartialAuthConfigError, resolve_auth_config
from nauro.store.config import load_config
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import NoProjectError, resolve_project_binding
from nauro.sync.generation_credentials import GenerationAuth, GenerationConnection
from nauro.sync.reference_auth import AUTH_ERRORS


def _origin(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Trusted HTTPS API origin required")
    return value.rstrip("/")


def selected_connection(redirect_uri: str) -> tuple[GenerationConnection, str] | None:
    try:
        binding = resolve_project_binding(project_id=None, cwd=str(Path.cwd()))
    except NoProjectError:
        return None
    if observe_generation_marker(binding) is None:
        return None
    domain, client, api, audience = resolve_auth_config(os.environ, load_config())
    if binding.server_url is None or _origin(binding.server_url) != _origin(api):
        raise ValueError("Project and trusted authentication endpoints differ")
    return GenerationConnection(
        endpoint=_origin(api) + "/mcp",
        issuer=f"https://{domain}/",
        client_id=client,
        audience=audience,
        redirect_uri=redirect_uri,
    ), binding.project_id


def run_generation_auth(
    action: str, redirect_uri: str, present_url: Callable[[str], None]
) -> str | None:
    try:
        selected = selected_connection(redirect_uri)
        if selected is None:
            return None
        connection, project = selected
        with httpx.Client(trust_env=False) as client:
            auth = GenerationAuth(connection, project, client)
            if action == "login":
                auth.login(present_url)
            elif action == "refresh":
                auth.refresh()
            elif action == "logout":
                auth.logout()
            elif action == "status":
                return auth.status()
            else:
                raise ValueError("Unsupported authentication action")
        return "Generation credentials updated. No decision was submitted."
    except (*AUTH_ERRORS, PartialAuthConfigError):
        raise ValueError(
            "Generation authentication failed. "
            "Check project settings and auth status, or log in again."
        ) from None
