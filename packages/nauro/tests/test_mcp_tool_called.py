"""Phase 1c T1.5 — mcp.tool_called decorator tests.

Covers:
1. All 11 canonical MCP tools emit one mcp.tool_called event per call with the
   right tool_name, transport (read from ContextVar), success, duration_bucket.
2. ContextVar plumbing — set_transport("http") flips transport, set_transport
   ("stdio") flips it back.
3. A tool that raises emits one event with success=False AND re-raises.
4. Property allowlist is closed — exactly four keys, never tool args, return
   values, project_id, or exception strings (D117 never-sent list).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_ALLOWED_KEYS = frozenset({"tool_name", "transport", "success", "duration_bucket"})
_DURATION_PATTERN = re.compile(r"^(<10ms|10-100ms|100ms-1s|1-10s|>10s)$")


class FakeClient:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None:
        self.events.append({"event": event, "distinct_id": distinct_id, "properties": properties})


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    return home


@pytest.fixture
def telemetry_enabled(nauro_home, monkeypatch):
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")
    aid = "11111111-1111-4111-8111-111111111111"
    (nauro_home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": aid,
                    "enabled": True,
                    "consent_version": 1,
                    "consented_at": "2026-04-30T00:00:00Z",
                }
            }
        )
    )
    return aid


@pytest.fixture
def fake_posthog(monkeypatch):
    import nauro.telemetry.client as client_mod

    fake = FakeClient()
    client_mod._client = fake
    yield fake
    client_mod._client = None


@pytest.fixture(autouse=True)
def _reset_transport_default():
    """Each test starts with a default ContextVar transport."""
    from nauro.telemetry.transport import set_transport

    set_transport("stdio")
    yield
    set_transport("stdio")


def _empty_store(home: Path) -> Path:
    """Build a minimally-functional store the read tools can resolve."""
    store = home / "projects" / "phase1c"
    (store / "decisions").mkdir(parents=True)
    (store / "snapshots").mkdir()
    (store / "project.md").write_text("# project\n")
    (store / "state.md").write_text("# state\n")
    (store / "stack.md").write_text("# stack\n")
    (store / "open-questions.md").write_text("# open\n")
    return store


def _assert_tool_called_shape(
    props: dict[str, Any], *, tool_name: str, transport: str, success: bool
) -> None:
    assert set(props.keys()) == _ALLOWED_KEYS, (
        f"unexpected keys: {set(props.keys()) - _ALLOWED_KEYS}"
    )
    assert props["tool_name"] == tool_name
    assert props["transport"] == transport
    assert props["success"] is success
    assert _DURATION_PATTERN.match(props["duration_bucket"]), props["duration_bucket"]


def test_get_context_emits_one_event_with_stdio_transport(
    nauro_home, telemetry_enabled, fake_posthog
):
    from nauro.mcp.tools import tool_get_context
    from nauro.telemetry.transport import set_transport

    set_transport("stdio")
    store = _empty_store(nauro_home)
    tool_get_context(store, "L0")

    tool_events = [e for e in fake_posthog.events if e["event"] == "mcp.tool_called"]
    assert len(tool_events) == 1
    _assert_tool_called_shape(
        tool_events[0]["properties"],
        tool_name="get_context",
        transport="stdio",
        success=True,
    )


def test_transport_http_propagates_to_decorator(nauro_home, telemetry_enabled, fake_posthog):
    """Validates ContextVar plumbing — the dispatcher-set value reaches the decorator."""
    from nauro.mcp.tools import tool_list_decisions
    from nauro.telemetry.transport import set_transport

    set_transport("http")
    store = _empty_store(nauro_home)
    tool_list_decisions(store)

    tool_events = [e for e in fake_posthog.events if e["event"] == "mcp.tool_called"]
    assert len(tool_events) == 1
    _assert_tool_called_shape(
        tool_events[0]["properties"],
        tool_name="list_decisions",
        transport="http",
        success=True,
    )


def test_failing_tool_emits_one_event_with_success_false(
    nauro_home, telemetry_enabled, fake_posthog
):
    """A tool that raises must emit success=False AND re-raise.

    Tool implementations in tools.py mostly return error dicts rather than
    raising; we exercise the decorator's exception path by wrapping a
    deliberately-raising function at runtime.
    """
    from nauro.telemetry.decorators import mcp_tool

    @mcp_tool("synthetic_failure")
    def _boom() -> None:
        raise RuntimeError("internal error with secret_payload")

    with pytest.raises(RuntimeError):
        _boom()

    tool_events = [e for e in fake_posthog.events if e["event"] == "mcp.tool_called"]
    assert len(tool_events) == 1
    props = tool_events[0]["properties"]
    assert props["tool_name"] == "synthetic_failure"
    assert props["success"] is False
    # No exception leakage in the event payload (D117 never-sent list).
    assert set(props.keys()) == _ALLOWED_KEYS
    assert "secret_payload" not in json.dumps(tool_events[0])


def test_event_properties_never_contain_args_or_returns(
    nauro_home, telemetry_enabled, fake_posthog
):
    """Exhaustive privacy assertion: tool args/returns/project_id are never in event props."""
    from nauro.mcp.tools import tool_search_decisions

    store = _empty_store(nauro_home)
    secret_query = "this-string-must-never-appear-in-telemetry"
    tool_search_decisions(store, secret_query, 1)

    tool_events = [e for e in fake_posthog.events if e["event"] == "mcp.tool_called"]
    assert len(tool_events) == 1
    serialized = json.dumps(tool_events[0])
    assert secret_query not in serialized
    assert str(store) not in serialized
    # Closed allowlist — any key drift is a privacy regression.
    assert set(tool_events[0]["properties"].keys()) == _ALLOWED_KEYS


def test_all_eleven_tools_decorated():
    """The 11-tool taxonomy is locked. New tools must opt into instrumentation."""
    from nauro.mcp import tools

    expected = {
        "tool_get_context",
        "tool_propose_decision",
        "tool_confirm_decision",
        "tool_check_decision",
        "tool_flag_question",
        "tool_get_raw_file",
        "tool_list_decisions",
        "tool_get_decision",
        "tool_diff_since_last_session",
        "tool_search_decisions",
        "tool_update_state",
    }
    actual = {n for n in dir(tools) if n.startswith("tool_")}
    assert expected <= actual, f"missing tools: {expected - actual}"
    # functools.wraps preserves the original __wrapped__ marker — fall back to a
    # name-based smoke test that the decorator was applied.
    for name in expected:
        fn = getattr(tools, name)
        assert hasattr(fn, "__wrapped__"), f"{name} is not decorated by mcp_tool"
