"""Diagnostic refresh attempts for installed generation replicas."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from nauro_core.provenance import validate_utc_timestamp
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI, PartialAuthConfigError
from nauro.store.generation_authority import RefreshRequiredError
from nauro.store.generation_refresh_io import (
    RefreshPaths,
    durable_replace,
    read_evidence,
    refresh_paths,
)
from nauro.store.generation_refresh_state import RefreshControlPair, _pointer
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.home import nauro_home
from nauro.store.replica_control import _native_control_lock, _validate_managed_path
from nauro.store.resolution import ResolvedProjectBinding, resolve_project_binding
from nauro.sync.generation_connection import connection_for
from nauro.sync.generation_credentials import AccountRecord, GenerationConnection
from nauro.sync.generation_refresh import recover_generation_refresh
from nauro.sync.generation_session import GenerationConnectionError, GenerationTransferSession
from nauro.sync.remote import TransferBoundaryError

REFRESH_FAILURES = (
    ValueError,
    OSError,
    PartialAuthConfigError,
    TransferBoundaryError,
    httpx.HTTPError,
)


class RefreshAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    version: Literal[1] = 1
    project_id: str
    actor: str
    connection: str
    attempted_at: str
    succeeded_at: str | None
    error_code: (
        Literal[
            "refresh_incomplete",
            "refresh_failed",
            "refresh_required",
            "generation_connection_unavailable",
        ]
        | None
    )

    @field_validator("attempted_at", "succeeded_at")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        return (
            validate_utc_timestamp(value, field="refresh timestamp") if value is not None else None
        )

    @model_validator(mode="after")
    def completed(self) -> RefreshAttempt:
        if self.error_code is None and (
            self.succeeded_at is None or self.succeeded_at < self.attempted_at
        ):
            raise ValueError("Refresh success needs a completion timestamp")
        return self


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _account(binding: ResolvedProjectBinding) -> tuple[GenerationConnection, AccountRecord]:
    try:
        if resolve_project_binding(binding.project_id, None, use_cwd=False) != binding:
            raise GenerationConnectionError("The project binding changed.")
        connection = connection_for(binding, DEFAULT_AUTH_REDIRECT_URI)
        with connection.store().locked():
            account = connection.store().read()
        if account is None or not account.user_id:
            raise GenerationConnectionError("Generation login required.")
        return connection, account
    except (ValueError, OSError, PartialAuthConfigError):
        raise GenerationConnectionError("Check generation login and project connection.") from None


def _attempt_path(
    binding: ResolvedProjectBinding, connection: GenerationConnection, actor: str
) -> Path:
    return (
        nauro_home()
        / f"generation-refresh-{binding.project_id}-{actor}-{connection.binding()}.json"
    )


def _read_attempt(
    binding: ResolvedProjectBinding, connection: GenerationConnection, actor: str
) -> RefreshAttempt | None:
    home = nauro_home()
    raw = read_evidence(RefreshPaths(home, home), _attempt_path(binding, connection, actor))
    if raw is None:
        return None
    try:
        attempt = RefreshAttempt.model_validate_json(raw)
    except ValueError:
        raise RefreshRequiredError("Retained refresh status is invalid.") from None
    if (attempt.project_id, attempt.actor, attempt.connection) != (
        binding.project_id,
        actor,
        connection.binding(),
    ) or attempt.model_dump_json().encode() != raw:
        raise RefreshRequiredError("Refresh status belongs to another binding.")
    return attempt


def refresh_replica(binding: ResolvedProjectBinding) -> GenerationSnapshotStore:
    connection, account = _account(binding)
    actor = account.user_id
    paths = refresh_paths(binding, actor)
    if not paths.actor.is_dir():
        raise RefreshRequiredError("An installed actor replica is required.")
    home = nauro_home()
    evidence = RefreshPaths(home, home)
    attempt_path = _attempt_path(binding, connection, actor)
    lock = attempt_path.with_suffix(".lock")
    _validate_managed_path(home, lock)
    with _native_control_lock(home, lock, 0):
        prior = _read_attempt(binding, connection, actor)
        attempt = RefreshAttempt(
            project_id=binding.project_id,
            actor=actor,
            connection=connection.binding(),
            attempted_at=_now(),
            succeeded_at=prior.succeeded_at if prior else None,
            error_code="refresh_incomplete",
        )

        def save(value: RefreshAttempt) -> None:
            selected, current = _account(binding)
            if selected != connection or current.user_id != actor:
                raise GenerationConnectionError("The refresh account changed.")
            value = RefreshAttempt.model_validate(value.model_dump())
            durable_replace(evidence, attempt_path, value.model_dump_json().encode())

        save(attempt)
        try:
            with GenerationTransferSession(binding) as session:
                if session.actor != actor or session.connection != connection:
                    raise GenerationConnectionError("The refresh account changed.")
                store = recover_generation_refresh(binding, actor=actor, session=session)
                session.credentials()
                save(attempt.model_copy(update={"succeeded_at": _now(), "error_code": None}))
                session.credentials()
                return store
        except Exception as exc:
            code = (
                "generation_connection_unavailable"
                if isinstance(exc, GenerationConnectionError)
                else "refresh_required"
                if isinstance(exc, RefreshRequiredError)
                else "refresh_failed"
            )
            save(attempt.model_copy(update={"error_code": code}))
            raise


def replica_status(binding: ResolvedProjectBinding) -> dict[str, object]:
    status: dict[str, object] = {
        "project_id": binding.project_id,
        "installed_for_user_id": None,
        "projection_class": None,
        "projection_scope_id": None,
        "store_format_version": None,
        "generation_id": None,
        "manifest_digest": None,
        "committed_at": None,
        "installed_at": None,
        "last_refresh_attempt_at": None,
        "last_refresh_succeeded_at": None,
        "last_refresh_error_code": None,
        "offline": None,
        "api_only_availability": None,
        "pending_outbox_count": None,
        "parked_outbox_count": None,
        "authorization_checked": False,
    }
    try:
        connection, account = _account(binding)
        actor = account.user_id
        paths = refresh_paths(binding, actor)
        status["installed_for_user_id"] = actor
        with _native_control_lock(paths.store, paths.store / ".replica-control.lock", 0):
            attempt = _read_attempt(binding, connection, actor)
            if attempt is not None:
                status.update(
                    last_refresh_attempt_at=attempt.attempted_at,
                    last_refresh_succeeded_at=attempt.succeeded_at,
                    last_refresh_error_code=attempt.error_code,
                )
            raw = read_evidence(paths, paths.pointer)
            carrier = read_evidence(paths, paths.carrier)
            if raw is None or carrier is None:
                raise RefreshRequiredError("Replica controls unavailable.")
            RefreshControlPair(raw, carrier)
            pointer = _pointer(raw)
            if pointer.project_id != binding.project_id or pointer.installed_for_user_id != actor:
                raise RefreshRequiredError("Replica controls differ from the account.")
            for name in (
                "projection_class",
                "projection_scope_id",
                "store_format_version",
                "generation_id",
                "manifest_digest",
                "committed_at",
                "installed_at",
            ):
                status[name] = getattr(pointer, name)
        selected, current = _account(binding)
        if selected != connection or current != account:
            raise GenerationConnectionError("The status account changed.")
        return status
    except REFRESH_FAILURES:
        return {
            "project_id": binding.project_id,
            "error_code": "replica_status_unavailable",
            "authorization_checked": False,
        }
