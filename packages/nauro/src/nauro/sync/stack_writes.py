"""Stack replacements from a locked generation session."""

from __future__ import annotations

from typing import Any, cast

from nauro.store import stack_records as records
from nauro.store.stack_contract import StackScope, stack_payload
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import stack_submission as submission
from nauro.sync.generation_refresh_status import REFRESH_FAILURES
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.stack_transport import HttpStackTransport
from nauro.sync.write_failures import write_failure
from nauro.sync.write_revision import capture_write_revision


def execute_stack_write(
    mode: str,
    content: dict[str, Any],
    operation_id: str | None,
    digest: str | None,
    session: GenerationTransferSession,
) -> dict[str, Any]:
    project, actor = session.binding.project_id, session.actor
    connection = session.connection.binding()
    auth = {"require_actor": session.require_actor}
    reference = {
        key: value
        for key, value in (("operation_id", operation_id), ("payload_digest", digest))
        if value is not None
    }
    try:
        if mode == "discover":
            return {
                "status": "discovered",
                "attempts": [
                    record.model_dump(mode="json")
                    for record in records.list_stack_submissions(project, actor, **auth)
                    if record.connection == connection
                ],
            }
        if mode == "submit":
            if "expected_revision" not in content:
                content["expected_revision"] = capture_write_revision(
                    session.binding, actor=actor, session=session, family="stack"
                )
            payload = stack_payload(**content)
            record = records.prepare_stack_submission(
                project, actor, payload, connection=connection, **auth
            )
        else:
            scope = StackScope(
                project_id=project, user_id=actor, operation_id=cast(str, operation_id)
            )
            saved = records.read_stack_submission(scope, **auth)
            if saved is None or saved.connection != connection or saved.payload_digest != digest:
                raise ValueError("The saved attempt does not match this connection and reference.")
            record = saved
    except (SubmissionRecordError, OSError) as error:
        return {**reference, **write_failure(error)}
    reference = {
        "operation_id": record.scope.operation_id,
        "payload_digest": record.payload_digest,
    }
    try:
        transport = HttpStackTransport(
            session.api_url,
            session.client,
            connection=session.connection,
            credentials=session.credentials,
            **auth,
        )
        result = getattr(submission, f"{mode}_stack")(record.scope, transport, **auth)
    except (SubmissionRecordError, *REFRESH_FAILURES) as error:
        return {**reference, **write_failure(error)}
    return {**result.model_dump(mode="json"), **reference}
