"""Prepared-reference delivery for normal generation-backed decision calls."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI, ActiveCredentials, PartialAuthConfigError
from nauro.store.resolution import resolve_project_binding
from nauro.sync.decision_reference import DecisionReferenceTransport
from nauro.sync.generation_connection import selected_connection
from nauro.sync.generation_credentials import GenerationConnection, generation_credentials
from nauro.sync.generation_refresh_status import REFRESH_FAILURES, refresh_replica, replica_status

REFUSED_STATUSES = {"stale", "unresolved", "pending", "expired", "conflict", "disposed"}
Selection = tuple[GenerationConnection, str]


def select_decision_connection(
    project: str | None = None, cwd: str | Path | None = None, *, use_cwd: bool = True
) -> Selection | None:
    try:
        return selected_connection(DEFAULT_AUTH_REDIRECT_URI, project, cwd, use_cwd=use_cwd)
    except PartialAuthConfigError:
        raise ValueError("Trusted authentication configuration is incomplete") from None


def reference_client() -> httpx.Client:
    return httpx.Client(trust_env=False)


class DecisionSession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: httpx.Client | None = None
        self._transport: DecisionReferenceTransport | None = None
        self._key: tuple[str, str, str] | None = None

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = self._transport = self._key = None

    def execute(
        self,
        selected: Selection,
        request: dict[str, Any],
        cwd: str | Path | None = None,
        *,
        use_cwd: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            connection, project = selected
            store = connection.store()
            with store.locked():
                record = store.read()
                if record is None:
                    raise ValueError("Run nauro auth login in this project")
                actor = record.user_id

            def credentials() -> ActiveCredentials:
                if select_decision_connection(project, cwd, use_cwd=use_cwd) != selected:
                    raise ValueError("Project authority or connection changed")
                return generation_credentials(connection, actor)

            key = (connection.binding(), project, actor)
            if self._key != key:
                self.close()
                self._client = reference_client()
                self._transport = DecisionReferenceTransport(
                    connection.endpoint, project, actor, self._client, credentials
                )
                self._key = key
            assert self._transport is not None
            self._transport.credentials = credentials
            binding = resolve_project_binding(project, cwd or Path.cwd(), use_cwd=use_cwd)
            result = self._transport.propose_decision(**request)
            if result.get("status") != "committed":
                return result
            try:
                if resolve_project_binding(project, cwd or Path.cwd(), use_cwd=use_cwd) != binding:
                    raise ValueError("Project binding changed")
                refresh_replica(binding, expected=(connection, actor))
                status = replica_status(binding)
                credentials()
                if status.get("installed_for_user_id") != actor:
                    raise ValueError("Refresh account changed")
            except REFRESH_FAILURES:
                status = {
                    "project_id": project,
                    "error_code": "receipt_refresh_required",
                    "authorization_checked": False,
                }
            return {**result, "replica_status": status}


def execute_decision(
    selected: Selection,
    request: dict[str, Any],
    cwd: str | Path | None = None,
    *,
    use_cwd: bool = True,
) -> dict[str, Any]:
    session = DecisionSession()
    try:
        return session.execute(selected, request, cwd, use_cwd=use_cwd)
    finally:
        session.close()
