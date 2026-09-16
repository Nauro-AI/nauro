"""Authenticated recovery observations and explicit exact-action dispatch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from nauro_core.identifiers import IdentifierKind, validate_identifier
from nauro_core.operations.commit_plan import canonical_judgment_payload_bytes

from nauro.auth import ActiveCredentials
from nauro.store.recovery_actions import RecoveryAction, timestamp
from nauro.store.submission_records import SubmissionScope
from nauro.sync.judgment_transport import _unique_object, _verify_receipt

MAX_RECOVERY_RESPONSE = 64 * 1024 * 1024


def _json(raw: bytes | str) -> dict[str, Any]:
    result = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise TypeError("Recovery response must be an object")
    return result


def _recovery_receipt(raw: str, action: str, saved: RecoveryAction | None) -> dict[str, Any]:
    receipt = _json(raw)
    fields = {
        "receipt_id",
        "operation_kind",
        "operation_id",
        "result",
        "committed_at",
        "target_kind",
        "target_id",
        "details",
    }
    if (
        set(receipt) != fields
        or canonical_judgment_payload_bytes(receipt).decode() != raw
        or receipt["operation_kind"] != "judgment_recovery"
        or receipt["operation_id"] != action
        or receipt["target_kind"] != "judgment_saga"
        or receipt["result"] not in {"resumed", "abandoned"}
    ):
        raise ValueError("Recovery receipt identity or bytes differ")
    for name in ("receipt_id", "target_id"):
        validate_identifier(IdentifierKind.ulid, receipt[name], field=name)
    timestamp(receipt["committed_at"])
    details = receipt["details"]
    expected = {"disposition", "source_fencing_token", "resulting_fencing_token"}
    active = details.get("source_state") == "active"
    if active:
        expected.add("source_state")
    disposition = "resume" if receipt["result"] == "resumed" else "abandon"
    source, target = details.get("source_fencing_token"), details.get("resulting_fencing_token")
    if (
        set(details) != expected
        or details["disposition"] != disposition
        or type(source) is not int
        or type(target) is not int
        or source < 1
        or target != source + int(disposition == "resume" or active)
    ):
        raise ValueError("Recovery receipt transition differs")
    if saved is not None:
        payload = saved.payload
        if (
            receipt["target_id"] != payload.saga_id
            or disposition != payload.disposition
            or source != payload.binding.expected_fence
            or active != (payload.binding.expected_state == "active")
        ):
            raise ValueError("Recovery receipt differs from the saved action")
    return receipt


def _inspection(result: dict[str, Any], project: str) -> None:
    if set(result) != {
        "kind",
        "lane",
        "saga",
        "saved_request",
        "execution_deadline",
        "current_state",
    }:
        raise ValueError("Invalid inspection fields")
    saga = result["saga"]
    if saga is not None and saga["project_id"] != project:
        raise ValueError("Inspected saga belongs to another project")
    lane = result["lane"]
    if lane is not None and lane["project_id"] != project:
        raise ValueError("Inspected lane belongs to another project")
    saved = result["saved_request"]
    if saved is not None and (
        saga is None
        or saved["project_id"] != project
        or saved["actor_id"] != saga["idempotency_user_id"]
        or saved["operation_id"] != saga["idempotency_operation_id"]
        or saved["payload_digest"] != saga["payload_digest"]
        or hashlib.sha256(saved["payload_json"].encode()).hexdigest() != saved["payload_digest"]
    ):
        raise ValueError("Original saved request does not bind the saga")
    if result["execution_deadline"] is not None:
        timestamp(result["execution_deadline"])
    state = result["current_state"]
    if state is not None and state.get("kind") == "committed":
        if saga is None:
            raise ValueError("Committed inspection lacks its saga")
        _verify_receipt(
            state["receipt_json"],
            SubmissionScope(
                project_id=project,
                user_id=saga["idempotency_user_id"],
                operation_id=saga["idempotency_operation_id"],
            ),
        )


class RecoveryTransport:
    def __init__(
        self,
        endpoint: str,
        project: str,
        actor: str,
        client: httpx.Client,
        credentials: Callable[[], ActiveCredentials],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        url = httpx.URL(endpoint)
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path != "/mcp"
        ):
            raise ValueError("Recovery requires the trusted generation endpoint")
        for value in (project, actor):
            validate_identifier(IdentifierKind.ulid, value, field="recovery_scope")
        self.url = str(url.copy_with(path="/judgments/recovery"))
        self.clock = clock
        self.project, self.actor, self.client, self.credentials = (
            project,
            actor,
            client,
            credentials,
        )

    def _credentials(self) -> ActiveCredentials:
        credentials = self.credentials()
        if credentials.user_id != self.actor:
            raise ValueError("Recovery actor changed")
        return credentials

    def _call(
        self, mode: str, saved: RecoveryAction | None = None, /, **fields: str
    ) -> dict[str, Any]:
        credentials = self._credentials()
        if saved is not None:
            saved.require_window(self.clock())
        try:
            with self.client.stream(
                "POST",
                self.url,
                json={
                    "version": 1,
                    "mode": mode,
                    "project_id": self.project,
                    "expected_user_id": self.actor,
                    **fields,
                },
                headers={"Authorization": f"Bearer {credentials.access_token}"},
                timeout=25,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ValueError(
                        f"Recovery returned HTTP {response.status_code}; lookup is required"
                    )
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RECOVERY_RESPONSE:
                        raise ValueError("Recovery response exceeds size limit")
        except httpx.HTTPError as exc:
            raise ValueError("Recovery outcome is unknown; look up the saved action") from exc
        self._credentials()
        envelope = _json(bytes(raw))
        if (
            set(envelope) != {"version", "mode", "result"}
            or type(envelope["version"]) is not int
            or envelope["version"] != 1
            or envelope["mode"] != mode
            or not isinstance(envelope["result"], dict)
        ):
            raise ValueError("Invalid recovery response envelope")
        return envelope["result"]

    def inspect(self, saga: str | None = None) -> dict[str, Any]:
        result = self._call("inspect", **({"saga_id": saga} if saga else {}))
        if result.get("kind") != "inspection":
            raise ValueError("Expected a recovery inspection")
        _inspection(result, self.project)
        if saga is not None and result["saga"] is not None and result["saga"]["saga_id"] != saga:
            raise ValueError("Inspected saga differs")
        return result

    def _action(
        self, result: dict[str, Any], action: str, saved: RecoveryAction | None = None
    ) -> None:
        scope = {
            "project_id": self.project,
            "user_id": self.actor,
            "operation_kind": "judgment_recovery",
            "operation_id": action,
        }
        if (
            set(result) != {"kind", "scope", "payload_digest", "receipt_json", "status", "judgment"}
            or result["kind"] != "action"
            or result["scope"] != scope
            or result["status"] not in {"accepted", "absent", "expired"}
        ):
            raise ValueError("Recovery action lookup binding differs")
        digest = result["payload_digest"]
        if saved is not None and digest is not None and digest != saved.payload_digest:
            raise ValueError("Recovery action digest differs")
        if result["status"] == "accepted":
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Accepted recovery lacks a digest")
            receipt = _recovery_receipt(result["receipt_json"], action, saved)
            judgment = result["judgment"]
            if (
                judgment is not None
                and judgment["saga"] is not None
                and judgment["saga"]["saga_id"] != receipt["target_id"]
            ):
                raise ValueError("Action inspection belongs to another saga")
        elif result["receipt_json"] is not None:
            raise ValueError("Unaccepted action carries a receipt")
        if result["judgment"] is not None:
            _inspection(result["judgment"], self.project)

    def lookup(self, action: str, saved: RecoveryAction | None = None) -> dict[str, Any]:
        result = self._call("lookup", action_id=action)
        self._action(result, action, saved)
        return result

    def discover(self, after: str | None = None) -> dict[str, Any]:
        result = self._call("discover", **({"after": after} if after else {}))
        if set(result) != {"kind", "actions", "next_after"} or result["kind"] != "page":
            raise ValueError("Invalid recovery discovery page")
        if not isinstance(result["actions"], list) or len(result["actions"]) > 10:
            raise ValueError("Invalid recovery discovery size")
        for action in result["actions"]:
            self._action(action, action["scope"]["operation_id"])
        identities = [action["scope"]["operation_id"] for action in result["actions"]]
        if identities != sorted(set(identities)) or (
            after is not None and any(value <= after for value in identities)
        ):
            raise ValueError("Discovery records did not advance")
        cursor = result["next_after"]
        if cursor is not None:
            validate_identifier(IdentifierKind.operation_id, cursor, field="cursor")
            if not identities or cursor != identities[-1]:
                raise ValueError("Discovery cursor does not follow its last returned action")
        return result

    def dispatch(self, saved: RecoveryAction) -> dict[str, Any]:
        if (saved.project_id, saved.actor_id) != (self.project, self.actor):
            raise ValueError("Recovery action scope differs")
        result = self._call(
            "dispatch",
            saved,
            action_id=saved.action_id,
            payload_digest=saved.payload_digest,
            action_payload=saved.action_payload,
        )
        kind = result.get("kind")
        if kind in {"resume_accepted", "abandon_accepted"}:
            receipt = _recovery_receipt(result["receipt_json"], saved.action_id, saved)
            if kind != receipt["details"]["disposition"] + "_accepted":
                raise ValueError("Recovery result and receipt disagree")
            state = result["current_state"]
            if state.get("kind") == "committed":
                _verify_receipt(state["receipt_json"], saved.payload.binding.original_scope)
        elif kind not in {
            "already_committed",
            "already_abandoned",
            "wrong_state",
            "not_found",
            "absent",
            "refused",
            "expired",
            "unresolved",
            "failed",
            "corrupt",
        }:
            raise ValueError("Unknown recovery outcome")
        return result
