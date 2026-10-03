"""Durable typed writes from an authenticated generation replica."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Any, cast

from nauro.store import state_records as records
from nauro.store.generation_store import GenerationSnapshotStore
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import StoreResolutionError, resolve_project_binding
from nauro.store.state_contract import StateScope, state_payload
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import state_submission as submission
from nauro.sync.generation_refresh_status import REFRESH_FAILURES, refresh_replica, replica_status
from nauro.sync.generation_session import GenerationConnectionError, GenerationTransferSession
from nauro.sync.question_writes import execute_question_write
from nauro.sync.share_writes import execute_share_write
from nauro.sync.stack_writes import execute_stack_write
from nauro.sync.state_transport import HttpStateTransport
from nauro.sync.write_arguments import validate_write_arguments
from nauro.sync.write_failures import write_failure
from nauro.sync.write_revision import capture_write_revision

WRITE_GUIDANCE = (
    "On generation replicas, submit creates and saves an immutable attempt. "
    "Discover lists this connection's local saved attempts. Recover only looks up the saved "
    "operation_id and payload_digest. Retry looks up first and resends the original payload "
    "only when absent and within its original 24-hour horizon. Reference modes cannot accept "
    "content. An uncertain result includes the reference: recover it before another write. "
    "A committed receipt with receipt_refresh_required is committed; refresh the replica."
)
STATE_WRITE_GUIDANCE = (
    "State submissions default to the installed replica's document revision. "
    "An explicit expected_revision overrides that default; retries keep the saved revision."
)

FAMILIES = {
    "update_state": "state",
    "flag_question": "question",
    "update_stack": "stack",
    "share_context": "share",
}
CONTENT = {
    "share": {"slug", "content", "pointer_kind", "summary"},
    "state": {"delta", "expected_revision"},
    "stack": {"content", "expected_revision"},
    "question": {"question", "context", "targets", "resolved_by"},
}


def generation_write(
    operation: str,
    arguments: dict[str, Any],
    *,
    use_cwd: bool = True,
    on_refreshed: Callable[[GenerationSnapshotStore], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    try:
        binding = resolve_project_binding(
            arguments.get("project_id"), arguments.get("cwd") or Path.cwd(), use_cwd=use_cwd
        )
    except StoreResolutionError:
        return None
    if observe_generation_marker(binding) is None:
        if operation in {"update_stack", "share_context"}:
            return None
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
    with ExitStack() as sessions:
        try:
            session = sessions.enter_context(GenerationTransferSession(binding))
        except GenerationConnectionError as error:
            if family != "share":
                raise
            reference = (
                {"operation_id": operation_id, "payload_digest": digest}
                if mode in {"recover", "retry"}
                else {}
            )
            failure = {**reference, **write_failure(error)}
            if not reference:
                failure["unresolved"] = False
                failure["guidance"] = (
                    "No sharing request was sent. Restore the account and project connection, "
                    "then repeat this mode."
                )
            return failure
        executor = {
            "share": execute_share_write,
            "stack": execute_stack_write,
            "question": execute_question_write,
        }.get(family)
        if executor is not None:
            output = executor(mode, content, operation_id, digest, session)
            return _finish_write(output, session, on_refreshed)
        return _execute(mode, content, operation_id, digest, session, on_refreshed)


def _execute(
    mode: str,
    content: dict[str, Any],
    operation_id: str | None,
    digest: str | None,
    session: GenerationTransferSession,
    on_refreshed: Callable[[GenerationSnapshotStore], dict[str, Any]] | None,
) -> dict[str, Any]:
    project, actor = session.binding.project_id, session.actor
    connection = session.connection.binding()
    auth = {"require_actor": session.require_actor}
    try:
        if mode == "discover":
            saved = records.list_state_submissions(project, actor, **auth)
            return {
                "status": "discovered",
                "attempts": [
                    record.model_dump(mode="json")
                    for record in saved
                    if record.connection == connection
                ],
            }
        if mode == "submit":
            if "expected_revision" not in content:
                content["expected_revision"] = capture_write_revision(
                    session.binding, actor=actor, session=session
                )
            record = records.prepare_state_submission(
                project, actor, state_payload(**content), connection=connection, **auth
            )
        else:
            scope = StateScope(
                project_id=project, user_id=actor, operation_id=cast(str, operation_id)
            )
            saved_record = records.read_state_submission(scope, **auth)
            if (
                saved_record is None
                or saved_record.connection != connection
                or saved_record.payload_digest != digest
            ):
                raise ValueError("The saved attempt does not match this connection and reference.")
            record = saved_record
    except (SubmissionRecordError, OSError, GenerationConnectionError) as error:
        reference = {
            key: value
            for key, value in (("operation_id", operation_id), ("payload_digest", digest))
            if value is not None
        }
        failure = {**reference, **write_failure(error)}
        if not reference:
            failure["unresolved"] = False
            failure["guidance"] = (
                "No request was sent. Restore account and local record access, then use discover "
                "to find any saved prepared attempt before submitting again."
            )
        return failure
    reference = {"operation_id": record.scope.operation_id, "payload_digest": record.payload_digest}
    try:
        transport = HttpStateTransport(
            session.api_url,
            session.client,
            connection=session.connection,
            credentials=session.credentials,
            **auth,
        )
        result = getattr(submission, f"{mode}_state")(record.scope, transport, **auth)
    except (SubmissionRecordError, *REFRESH_FAILURES) as error:
        return {**reference, **write_failure(error)}
    output = {**result.model_dump(mode="json"), **reference}
    return _finish_write(output, session, on_refreshed)


def _finish_write(
    output: dict[str, Any],
    session: GenerationTransferSession,
    on_refreshed: Callable[[GenerationSnapshotStore], dict[str, Any]] | None,
) -> dict[str, Any]:
    if output.get("status") == "refused":
        output["error_code"] = output["server_code"]
        output["guidance"] = (
            "Restore the refused request's authorization, project admission, or payload. "
            + (
                "The original write outcome remains unknown. Then recover this reference "
                "before any new write; do not resend while access is refused."
                if output["unresolved"]
                else "This attempt did not write. Correct the refusal before a new attempt."
            )
        )
    if output.get("status") == "slug_conflict_observed":
        output["guidance"] = (
            "The slug is occupied, but this saved attempt remains unresolved. "
            "Recover its original reference and reconcile the outcome before another operation. "
            "Do not change the slug or payload of this attempt."
        )
    if output.get("status") == "revision_conflict_observed":
        output["guidance"] = (
            "Refresh the replica and re-read the current document before preparing a new write. "
            "This saved attempt retains its original revision."
        )
    if output.get("status") == "committed":
        output.update(_refresh_committed(session, on_refreshed))
    return output


def _refresh_committed(
    session: GenerationTransferSession,
    on_refreshed: Callable[[GenerationSnapshotStore], dict[str, Any]] | None,
) -> dict[str, Any]:
    actor = session.actor
    try:
        session.require_binding(session.binding)
        snapshot = refresh_replica(session.binding, expected=(session.connection, actor))
        status = replica_status(session.binding)
        session.credentials()
        if status.get("installed_for_user_id") != actor:
            raise ValueError("The refresh account changed.")
    except REFRESH_FAILURES:
        return {
            "replica_status": {
                "error_code": "receipt_refresh_required",
                "authorization_checked": False,
            }
        }
    output = {"replica_status": status}
    if on_refreshed is not None:
        output["guidance_status"] = _regenerate_guidance(snapshot, on_refreshed)
    return output


def _regenerate_guidance(
    snapshot: GenerationSnapshotStore,
    callback: Callable[[GenerationSnapshotStore], dict[str, Any]],
) -> dict[str, Any]:
    # Derived guidance must not hide an already committed receipt.
    with suppress(Exception):
        return callback(snapshot)
    return {
        "status": "failed",
        "message": "Replica refresh completed, but guidance regeneration failed. "
        "Run 'nauro sync' to regenerate guidance. Do not resubmit the write.",
    }
