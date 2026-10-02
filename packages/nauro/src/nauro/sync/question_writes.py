"""Question append and resolution from a locked generation session."""

from __future__ import annotations

from typing import Any, cast

from nauro.store import question_records as records
from nauro.store.question_contract import QuestionScope, question_payload, resolution_payload
from nauro.store.submission_records import SubmissionRecordError
from nauro.sync import question_submission as submission
from nauro.sync.generation_refresh_status import REFRESH_FAILURES
from nauro.sync.generation_session import GenerationTransferSession
from nauro.sync.question_transport import HttpQuestionTransport
from nauro.sync.write_failures import write_failure


def execute_question_write(
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
                    for record in records.list_question_submissions(project, actor, **auth)
                    if record.connection == connection
                ],
            }
        if mode == "submit":
            targets = tuple(content.get("targets") or ())
            payload = (
                resolution_payload(targets, content["resolved_by"])
                if content.get("resolved_by") is not None
                else question_payload(content["question"], content.get("context"), targets)
            )
            record = records.prepare_question_submission(
                project, actor, payload, connection=connection, **auth
            )
        else:
            scope = QuestionScope(
                project_id=project, user_id=actor, operation_id=cast(str, operation_id)
            )
            saved = records.read_question_submission(scope, **auth)
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
        transport = HttpQuestionTransport(
            session.api_url,
            session.client,
            connection=session.connection,
            credentials=session.credentials,
            **auth,
        )
        result = getattr(submission, f"{mode}_question")(record.scope, transport, **auth)
    except (SubmissionRecordError, *REFRESH_FAILURES) as error:
        return {**reference, **write_failure(error)}
    return {**result.model_dump(mode="json"), **reference}
