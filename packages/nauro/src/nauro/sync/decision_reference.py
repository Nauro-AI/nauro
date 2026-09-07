"""Explicit decision-reference MCP transport; not selected by normal startup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from nauro_core.identifiers import IdentifierKind, validate_identifier

from nauro.auth import ActiveCredentials, read_active_credentials
from nauro.sync.decision_reference_contract import (
    MAX_RESPONSE,
    _json,
    reference_schema,
    validate_arguments,
    verify_observation,
)
from nauro.sync.decision_reference_contract import (
    DecisionReferenceError as DecisionReferenceError,
)
from nauro.sync.judgment_transport import JudgmentTransportError


class DecisionReferenceTransport:
    def __init__(
        self,
        endpoint: str,
        project: str,
        actor: str,
        client: httpx.Client,
        credentials: Callable[[], ActiveCredentials] = read_active_credentials,
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
            raise DecisionReferenceError("An operator-configured HTTPS MCP endpoint is required")
        for value in (project, actor):
            validate_identifier(IdentifierKind.ulid, value, field="reference_scope")
        self.endpoint, self.project, self.actor = endpoint, project, actor
        self.client, self.credentials = client, credentials
        self._sequence = 0
        self._initialized = False

    def _credentials(self) -> ActiveCredentials:
        credentials = self.credentials()
        if credentials.user_id != self.actor:
            raise DecisionReferenceError("Active account differs from the configured actor")
        return credentials

    def _rpc(self, method: str, params: dict[str, Any], *, notification: bool = False) -> Any:
        self._sequence += 1
        request_id = self._sequence
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = request_id
        credentials = self._credentials()
        try:
            with self.client.stream(
                "POST",
                self.endpoint,
                json=body,
                timeout=25,
                follow_redirects=False,
                headers={
                    "Authorization": "Bearer " + credentials.access_token,
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
            ) as response:
                if notification and response.status_code in {200, 202, 204}:
                    return None
                if response.status_code != 200:
                    raise DecisionReferenceError(
                        "No verified result. Use discovery or recovery; do not automatically resend"
                    )
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise DecisionReferenceError("Response exceeds the byte limit")
            self._credentials()
            result = _json(bytes(raw))
            if (
                result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not int
                or result.get("id") != request_id
                or "error" in result
            ):
                raise ValueError("Invalid RPC response")
            return result["result"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise DecisionReferenceError(
                "No verified result. Discover or recover the original request"
            ) from exc

    def initialize(self) -> None:
        if self._initialized:
            return
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "nauro-decision-reference", "version": "1"},
            },
        )
        if result.get("protocolVersion") != "2025-06-18":
            raise DecisionReferenceError("Unsupported MCP protocol")
        self._rpc("notifications/initialized", {}, notification=True)
        tools = self._rpc("tools/list", {})["tools"]
        if [tool["name"] for tool in tools] != ["propose_decision"] or tools[0].get(
            "inputSchema"
        ) != reference_schema():
            raise DecisionReferenceError("Unexpected isolated tool registry")
        self._initialized = True

    def propose_decision(self, **arguments: Any) -> dict[str, Any]:
        mode = validate_arguments(arguments, self.project)
        self.initialize()
        result = self._rpc("tools/call", {"name": "propose_decision", "arguments": arguments})
        try:
            if len(result["content"]) != 1 or result["content"][0]["type"] != "text":
                raise ValueError("Invalid tool result")
            value = _json(result["content"][0]["text"])
            if not isinstance(value, dict):
                raise ValueError("Tool result must be an object")
            if mode == "discover":
                if (
                    set(value) != {"version", "requests", "next_after"}
                    or type(value["version"]) is not int
                    or value["version"] != 1
                    or not isinstance(value["requests"], list)
                    or len(value["requests"]) > 1
                ):
                    raise ValueError("Invalid discovery page")
                if value["next_after"] is not None and not isinstance(value["next_after"], str):
                    raise ValueError("Invalid next cursor")
                observations = value["requests"]
            else:
                observations = [value]
            for observation in observations:
                verify_observation(observation, self.project, self.actor)
                if mode in {"submit", "recover", "retry"}:
                    for key in ("operation_id", "payload_digest"):
                        if observation["request"][key] != arguments[key]:
                            raise ValueError("Selected reference mismatch")
            return value
        except (ValueError, KeyError, TypeError, AttributeError, JudgmentTransportError) as exc:
            raise DecisionReferenceError("Invalid saved request or receipt evidence") from exc
