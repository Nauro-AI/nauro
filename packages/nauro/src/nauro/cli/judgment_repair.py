"""Owner-confirmed recovery through the existing repair command."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import typer
from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.auth import DEFAULT_AUTH_REDIRECT_URI, ActiveCredentials
from nauro.store.recovery_actions import (
    RecoveryAction,
    RecoveryActionStore,
    RecoveryPayload,
    timestamp,
)
from nauro.sync.generation_connection import attachment_connection
from nauro.sync.generation_credentials import generation_credentials
from nauro.sync.recovery_transport import RecoveryTransport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _show(label: str, value: object) -> None:
    if isinstance(value, dict) and value.get("judgment") is not None:
        value = {**value, "judgment": _inspection_display(value["judgment"])}
    typer.echo(label)
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


def _inspection_display(result: dict[str, Any]) -> dict[str, Any]:
    shown = dict(result)
    if shown.get("saga") is not None:
        shown["saga"] = {
            key: value for key, value in shown["saga"].items() if key != "plan_record_bytes"
        }
    return shown


def _discover(transport: RecoveryTransport) -> None:
    after = None
    for _ in range(100):
        page = transport.discover(after)
        cursor = page["next_after"]
        if cursor is not None and after is not None and cursor <= after:
            raise ValueError("Discovery cursor did not advance")
        _show("Server accepted actions (discovery is not a snapshot):", page)
        if cursor is None:
            return
        if not typer.confirm("Read the next action page?", default=False):
            return
        after = cursor
    typer.echo("Discovery stopped at 100 pages. This is not proof of absence.")


def _prepare(
    observed: dict[str, Any], store: RecoveryActionStore, disposition: str
) -> RecoveryAction:
    saga = observed["saga"]
    if saga is None or saga["status"] not in {"active", "recovery_required"}:
        raise ValueError("No eligible retained judgment to repair")
    now = _now()
    if saga["status"] == "active" and timestamp(saga["lease_expires_at"]) > now:
        raise ValueError("The recorded worker lease is still active")
    deadline = observed["execution_deadline"]
    if saga["idempotency_operation_id"].startswith("decision-request:") and deadline is None:
        raise ValueError("The original execution deadline is unavailable")
    payload = RecoveryPayload.model_validate(
        {
            "schema": "nauro.judgment_recovery.v2",
            "saga_id": saga["saga_id"],
            "disposition": disposition,
            "binding": {
                "original_scope": {
                    "project_id": store.project,
                    "user_id": saga["idempotency_user_id"],
                    "operation_kind": "judgment_commit",
                    "operation_id": saga["idempotency_operation_id"],
                },
                "original_payload_digest": saga["payload_digest"],
                "expected_state": saga["status"],
                "expected_fence": saga["fencing_token"],
                "expected_lease_owner": saga["lease_owner"],
                "expected_lease_expires_at": saga["lease_expires_at"],
                "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "admission_deadline": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            },
        }
    ).canonical()
    action = RecoveryAction(
        connection_binding=store.binding,
        project_id=store.project,
        actor_id=store.actor,
        action_id="recovery:" + uuid.uuid4().hex,
        action_payload=payload,
        payload_digest=hashlib.sha256(payload.encode()).hexdigest(),
        execution_deadline=deadline,
    )
    action.require_window(now)
    return action


def _send(
    transport: RecoveryTransport,
    store: RecoveryActionStore,
    action: RecoveryAction,
    *,
    resend: bool,
) -> None:
    result = transport.lookup(action.action_id, action)
    if result["status"] != "absent":
        _show("Server action (inspection does not resume execution):", result)
        judgment = result.get("judgment") or {}
        state = judgment.get("current_state") or {}
        if result["status"] != "accepted" or state.get("kind") not in {"committed", "abandoned"}:
            raise typer.Exit(1)
        return
    action.require_window(_now())
    _show("Exact recovery action:", action.model_dump())
    typer.echo("Absence does not cancel a delayed request. A competing action can win.")
    if action.payload.disposition == "abandon":
        typer.echo(
            "Abandon releases this judgment's reservations. Its consumed number and history remain."
        )
    if not typer.confirm(
        "Resend this saved action?" if resend else "Apply this recovery action?", default=False
    ):
        typer.echo("Nothing sent.")
        return
    transport._credentials()
    action.require_window(_now())
    if not resend:
        store.save(action)
    saved = store.read(action.action_id)
    if saved != action:
        raise ValueError("Saved recovery action changed")
    typer.echo(f"Saved action: {action.action_id}")
    typer.echo(
        f"Inspect with: nauro repair --project {store.project} "
        f"--judgment --action {action.action_id}"
    )
    action.require_window(_now())
    outcome = transport.dispatch(saved)
    _show("Recovery result (acceptance is not execution completion):", outcome)
    if outcome["kind"] not in {
        "resume_accepted",
        "abandon_accepted",
        "already_committed",
        "already_abandoned",
    }:
        raise typer.Exit(1)
    if outcome["kind"] in {"resume_accepted", "abandon_accepted"} and outcome["current_state"][
        "kind"
    ] not in {"committed", "abandoned"}:
        raise typer.Exit(1)


def run(
    project: str,
    saga: str | None,
    resume: bool,
    abandon: bool,
    action: str | None,
    resend: str | None,
) -> None:
    if os.name != "posix":
        raise ValueError("Hosted recovery requires POSIX durable private storage")
    validate_identifier(IdentifierKind.ulid, project, field="project")
    connection = attachment_connection(DEFAULT_AUTH_REDIRECT_URI)
    account = connection.store()
    with account.locked():
        record = account.read()
        if record is None:
            raise ValueError("Run nauro auth login for this generation endpoint")
        actor = record.user_id

    def credentials() -> ActiveCredentials:
        if attachment_connection(DEFAULT_AUTH_REDIRECT_URI) != connection:
            raise ValueError("The trusted connection changed")
        return generation_credentials(connection, actor)

    credentials()
    store = RecoveryActionStore(connection.binding(), project, actor)
    with httpx.Client(trust_env=False) as client:
        transport = RecoveryTransport(
            connection.endpoint, project, actor, client, credentials, clock=lambda: _now()
        )
        if action:
            try:
                saved = store.read(action)
            except FileNotFoundError:
                saved = None
            if saved is not None:
                _show("Local saved action (acceptance not inferred):", saved.model_dump())
            _show("Server action (lookup only):", transport.lookup(action, saved))
        elif resend:
            _send(transport, store, store.read(resend), resend=True)
        else:
            observed = transport.inspect(saga)
            _show("Hosted observation (lookup only):", _inspection_display(observed))
            if resume or abandon:
                _send(
                    transport,
                    store,
                    _prepare(observed, store, "resume" if resume else "abandon"),
                    resend=False,
                )
            else:
                _show(
                    "Local saved actions (acceptance not inferred):",
                    [record.model_dump() for record in store.list()],
                )
                _discover(transport)
