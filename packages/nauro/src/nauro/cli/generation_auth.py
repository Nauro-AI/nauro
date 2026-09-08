"""Select normal authentication from the current project's authority."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from nauro.auth import PartialAuthConfigError
from nauro.sync.generation_connection import selected_connection
from nauro.sync.generation_credentials import GenerationAuth
from nauro.sync.reference_auth import AUTH_ERRORS


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
