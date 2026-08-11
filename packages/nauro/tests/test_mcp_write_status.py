"""Unit pins for the stdio write-status renderer.

``render_write_status`` is the one place the stdio transport turns a write
envelope into the single line it is allowed to return. The parity suites
drive it through the real adapters; these tests drive it directly with
synthetic envelopes, which is the only way to reach a status no adapter
emits today.
"""

from __future__ import annotations

import pytest

from nauro.mcp.write_status import render_write_status


def _success() -> str:
    return "Committed."


def _explode() -> str:
    raise AssertionError("the success renderer ran off the ok path")


def test_ok_returns_the_wrapper_success_line():
    assert render_write_status({"store": "local", "status": "ok"}, _success) == "Committed."


def test_an_envelope_with_no_status_is_not_a_success():
    """Every adapter stamps a status. One that does not is a broken envelope."""
    assert render_write_status({"store": "local"}, _explode) == (
        "The envelope carried no write status. Treat the write as unconfirmed."
    )


def test_an_envelope_with_no_status_still_carries_its_text():
    envelope = {"store": "local", "guidance": "Run 'nauro init <name>'."}
    assert render_write_status(envelope, _explode) == (
        "The envelope carried no write status. Treat the write as unconfirmed. "
        "Run 'nauro init <name>'."
    )


def test_a_rejection_falls_back_to_its_remedial_guidance():
    """``error.guidance`` is read when the payload carries no reason text."""
    envelope = {
        "store": "local",
        "status": "rejected",
        "error": {"kind": "rejected", "reason": "", "guidance": "Shorten the delta."},
    }
    assert render_write_status(envelope, _explode) == "Shorten the delta."


def test_rejected_returns_the_error_reason_alone():
    envelope = {
        "store": "local",
        "status": "rejected",
        "error": {"kind": "rejected", "reason": "Delta exceeds 8000 characters."},
    }
    assert render_write_status(envelope, _explode) == "Delta exceeds 8000 characters."


def test_error_returns_the_guidance():
    envelope = {"store": "local", "status": "error", "guidance": "Run 'nauro init <name>'."}
    assert render_write_status(envelope, _explode) == "Run 'nauro init <name>'."


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("rejected", "The write was rejected. Nothing was recorded."),
        (
            "noop",
            "No write was made. The store has no state file to update. "
            "Run 'nauro status' for the store path, then restore state_current.md there.",
        ),
        ("error", "The write did not run. Nothing was recorded."),
    ],
)
def test_a_silent_envelope_still_names_the_outcome(status, expected):
    """No reason and no guidance still yields a line about the write."""
    assert render_write_status({"store": "local", "status": status}, _explode) == expected


def test_an_unknown_status_names_itself():
    envelope = {"store": "local", "status": "quarantined"}
    assert render_write_status(envelope, _explode) == (
        "Unexpected write status 'quarantined'. Treat the write as unconfirmed."
    )


def test_an_unknown_status_carries_whatever_text_it_has():
    envelope = {
        "store": "local",
        "status": "quarantined",
        "error": {"kind": "error", "reason": "The record awaits review."},
    }
    assert render_write_status(envelope, _explode) == (
        "Unexpected write status 'quarantined'. Treat the write as unconfirmed. "
        "The record awaits review."
    )


def test_the_assessment_rides_along_on_the_success_line():
    envelope = {"store": "local", "status": "ok", "assessment": "  Warning: push failed"}
    assert render_write_status(envelope, _success) == "Committed.\n\n  Warning: push failed"


def test_an_unrelated_envelope_key_is_ignored():
    """Per-tool payload must not reach the status dispatch."""
    envelope = {"store": "local", "status": "ok", "warning": "overlap", "project": {"id": "X"}}
    assert render_write_status(envelope, _success) == "Committed."
