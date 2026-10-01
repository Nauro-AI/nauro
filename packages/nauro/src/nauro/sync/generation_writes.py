"""Durable typed writes from an authenticated generation replica."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import StoreResolutionError, resolve_project_binding
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync.generation_refresh_status import REFRESH_FAILURES, refresh_replica, replica_status
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.write_arguments import validate_write_arguments
from nauro.sync.write_failures import write_failure

WRITE_GUIDANCE = (
    "On generation replicas, submit creates and saves an immutable attempt. "
    "Discover lists this connection's local saved attempts. Recover only looks up the saved "
    "operation_id and payload_digest. Retry looks up first and resends the original payload "
    "only when absent and within its original 24-hour horizon. Reference modes cannot accept "
    "content. An uncertain result includes the reference: recover it before another write. "
    "A committed receipt with receipt_refresh_required is committed; refresh the replica."
)

FAMILIES = {
    "update_state": "state",
    "flag_question": "question",
    "update_stack": "stack",
    "share_context": "share",
}
CONTENT = {
    "state": {"delta", "expected_revision"},
    "question": {"question", "context", "targets", "resolved_by"},
    "stack": {"content", "expected_revision"},
    "share": {"slug", "content", "pointer_kind", "summary"},
}


def _payload(family: str, content: dict[str, Any]) -> bytes:
    contract = import_module(f"nauro.store.{family}_contract")
    if family == "question":
        targets = tuple(content.pop("targets", ()))
        if content.get("resolved_by") is not None:
            if content.get("question") is not None or content.get("context") is not None:
                raise ValueError("Resolution cannot replace question content.")
            return cast(bytes, contract.resolution_payload(targets, content["resolved_by"]))
        return cast(bytes, contract.question_payload(targets=targets, **content))
    return cast(bytes, getattr(contract, f"{family}_payload")(**content))


def generation_write(
    operation: str,
    arguments: dict[str, Any],
    *,
    on_refreshed: Callable[[GenerationSnapshotStore], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    try:
        binding = resolve_project_binding(
            arguments.get("project_id"), arguments.get("cwd") or Path.cwd()
        )
    except StoreResolutionError:
        return None
    if observe_generation_marker(binding) is None:
        if any(
            arguments.get(key) is not None
            for key in ("request_mode", "operation_id", "payload_digest", "expected_revision")
        ):
            raise ValueError("Typed write modes require a generation replica.")
        return None
    validate_write_arguments(operation, arguments)
    family = FAMILIES[operation]
    mode = arguments.get("request_mode") or "submit"
    content = {key: arguments[key] for key in CONTENT[family] if arguments.get(key) is not None}
    operation_id, digest = arguments.get("operation_id"), arguments.get("payload_digest")
    with GenerationTransferSession(binding) as session:
        return _execute(family, mode, content, operation_id, digest, session, on_refreshed)


def _execute(
    family: str,
    mode: str,
    content: dict[str, Any],
    operation_id: str | None,
    digest: str | None,
    session: GenerationTransferSession,
    on_refreshed: Callable[[GenerationSnapshotStore], dict[str, Any]] | None,
) -> dict[str, Any]:
    records = import_module(f"nauro.store.{family}_records")
    submission = import_module(f"nauro.sync.{family}_submission")
    transports = import_module(f"nauro.sync.{family}_transport")
    project, actor = session.binding.project_id, session.actor
    connection = session.connection.binding()
    auth = {"require_actor": session.require_actor}
    if mode == "discover":
        saved = getattr(records, f"list_{family}_submissions")(project, actor, **auth)
        return {
            "status": "discovered",
            "attempts": [
                record.model_dump(mode="json")
                for record in saved
                if record.connection == connection
            ],
        }
    if mode == "submit":
        record = getattr(records, f"prepare_{family}_submission")(
            project, actor, _payload(family, content), connection=connection, **auth
        )
    else:
        contract = import_module(f"nauro.store.{family}_contract")
        scope = getattr(contract, family.title() + "Scope")(
            project_id=project, user_id=actor, operation_id=operation_id
        )
        record = getattr(records, f"read_{family}_submission")(scope, **auth)
        if record is None or record.connection != connection or record.payload_digest != digest:
            raise ValueError("The saved attempt does not match this connection and reference.")
    reference = {"operation_id": record.scope.operation_id, "payload_digest": record.payload_digest}
    transport = getattr(transports, "Http" + family.title() + "Transport")(
        session.api_url,
        session.client,
        credentials=session.credentials,
        **auth,
    )
    try:
        result = getattr(submission, f"{mode}_{family}")(record.scope, transport, **auth)
    except (SubmissionRecordError, *REFRESH_FAILURES) as error:
        return {**reference, **write_failure(family, error)}
    output = {**result.model_dump(mode="json"), **reference}
    if result.status == "committed":
        try:
            session.require_binding(session.binding)
            snapshot = refresh_replica(session.binding, expected=(session.connection, actor))
            status = replica_status(session.binding)
            session.credentials()
            if status.get("installed_for_user_id") != actor:
                raise ValueError("The refresh account changed.")
            output["replica_status"] = status
        except REFRESH_FAILURES:
            output["replica_status"] = {
                "error_code": "receipt_refresh_required",
                "authorization_checked": False,
            }
        else:
            if on_refreshed is not None:
                output["guidance_status"] = on_refreshed(snapshot)
    return output
