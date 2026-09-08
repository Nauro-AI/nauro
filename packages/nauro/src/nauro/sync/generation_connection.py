"""Bind generation delivery to the resolved project and trusted connection."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from nauro.auth import resolve_auth_config
from nauro.store.config import load_config
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import NoProjectError, ResolvedProjectBinding, resolve_project_binding
from nauro.sync.generation_credentials import GenerationConnection


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


def selected_connection(
    redirect_uri: str,
    project_id: str | None = None,
    cwd: str | Path | None = None,
    *,
    use_cwd: bool = True,
) -> tuple[GenerationConnection, str] | None:
    try:
        binding = resolve_project_binding(
            project_id=project_id, cwd=cwd or Path.cwd(), use_cwd=use_cwd
        )
    except NoProjectError:
        return None
    if observe_generation_marker(binding) is None:
        return None
    return connection_for(binding, redirect_uri), binding.project_id


def connection_for(binding: ResolvedProjectBinding, redirect_uri: str) -> GenerationConnection:
    if observe_generation_marker(binding) is None:
        raise ValueError("Generation authority required")
    domain, client, api, audience = resolve_auth_config(os.environ, load_config())
    if binding.server_url is None or _origin(binding.server_url) != _origin(api):
        raise ValueError("Project and trusted authentication endpoints differ")
    return GenerationConnection(
        endpoint=_origin(api) + "/mcp",
        issuer=f"https://{domain}/",
        client_id=client,
        audience=audience,
        redirect_uri=redirect_uri,
    )
