"""The render front door owns the envelope prologue every read-tool renderer shares."""

import pytest
from nauro_core.renderers import (
    _BODIES,
    RENDERERS,
    disconnected_reason_code,
    render,
    render_get_decision,
    render_list_projects,
)


def test_public_renderers_and_front_door_agree():
    envelope = {"content": "# 007 - Title\n\nbody"}
    assert render("get_decision", envelope, mode="header") == render_get_decision(
        envelope, mode="header"
    )
    assert set(RENDERERS) == set(_BODIES)


def test_unknown_tool_is_a_typed_error():
    with pytest.raises(ValueError, match="no renderer for tool"):
        render("teleport", {})


def test_disconnected_guidance_wins_only_when_present():
    disconnected = {"status": "error", "reason_code": "disconnected", "guidance": "  Run link  "}
    assert render("list_decisions", disconnected) == "Run link"
    without_guidance = {"status": "error", "reason_code": "disconnected", "error": "boom"}
    assert render("list_decisions", without_guidance) == "Error: boom"


def test_non_string_envelope_fields_are_ignored_not_raised():
    envelope = {"status": 7, "reason_code": ["x"], "guidance": None, "decisions": []}
    assert disconnected_reason_code(envelope) is None
    assert render("list_decisions", envelope) == "No decisions recorded yet."


def test_error_key_presence_renders_the_error_line_even_when_null():
    assert render("get_context", {"error": None}) == "Error: None"


def test_list_projects_error_envelope_renders_the_error_instead_of_an_empty_list():
    assert render_list_projects({"error": {"reason": "token expired"}}) == "Error: token expired"


def test_only_get_raw_file_renders_the_available_files_hint():
    envelope = {"error": "boom", "available_files": ["a.md"]}
    assert render("list_decisions", envelope) == "Error: boom"
    assert render("get_raw_file", envelope) == "Error: boom\n\nAvailable files:\n  - a.md"
