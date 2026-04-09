"""Tests for nauro_core.state — split state file logic."""

import re
from unittest.mock import patch

from nauro_core.state import (
    StateUpdateResult,
    assemble_state_for_context,
    migrate_legacy_state,
    prepare_state_update,
)

ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")


class TestPrepareStateUpdate:
    def test_with_existing_current(self):
        old = "# Current State\n\nShipping v1\n\n*Last updated: 2026-04-01T10:00Z*\n"
        result = prepare_state_update("Working on v2", old)

        assert isinstance(result, StateUpdateResult)
        assert "# Current State" in result.current_content
        assert "Working on v2" in result.current_content
        assert "*Last updated:" in result.current_content
        assert result.history_entry is not None
        assert "Shipping v1" in result.history_entry
        assert "---" in result.history_entry

    def test_first_write_no_prior(self):
        result = prepare_state_update("Initial state", None)

        assert "# Current State" in result.current_content
        assert "Initial state" in result.current_content
        assert result.history_entry is None

    def test_timestamp_format(self):
        result = prepare_state_update("test", None)
        match = re.search(r"\*Last updated: (.+?)\*", result.current_content)
        assert match is not None
        assert ISO_TIMESTAMP_RE.match(match.group(1))

    def test_history_entry_timestamp_format(self):
        old = "# Current State\n\nOld state\n\n*Last updated: 2026-04-01T10:00Z*\n"
        result = prepare_state_update("new", old)
        assert result.history_entry is not None
        # History entry starts with ## {timestamp}
        header_line = result.history_entry.split("\n")[0]
        ts = header_line.lstrip("# ").strip()
        assert ISO_TIMESTAMP_RE.match(ts)

    def test_history_entry_separator(self):
        old = "# Current State\n\nOld state\n\n*Last updated: 2026-04-01T10:00Z*\n"
        result = prepare_state_update("new", old)
        assert result.history_entry is not None
        assert result.history_entry.rstrip().endswith("---")

    def test_strips_header_and_footer_from_history(self):
        old = "# Current State\n\nThe actual content\n\n*Last updated: 2026-04-01T10:00Z*\n"
        result = prepare_state_update("new", old)
        assert result.history_entry is not None
        assert "# Current State" not in result.history_entry
        assert "*Last updated:" not in result.history_entry
        assert "The actual content" in result.history_entry

    def test_empty_current_content_no_history(self):
        old = "# Current State\n\n\n\n*Last updated: 2026-04-01T10:00Z*\n"
        result = prepare_state_update("new", old)
        # Empty body after stripping → no history entry
        assert result.history_entry is None

    def test_consistent_timestamps(self):
        """Both current and history use the same timestamp."""
        with patch("nauro_core.state._utc_timestamp", return_value="2026-04-08T15:30Z"):
            old = "# Current State\n\nOld\n\n*Last updated: 2026-04-01T10:00Z*\n"
            result = prepare_state_update("new", old)
            assert "2026-04-08T15:30Z" in result.current_content
            assert result.history_entry is not None
            assert "2026-04-08T15:30Z" in result.history_entry


class TestMigrateLegacyState:
    def test_returns_content_as_current(self):
        legacy = "# State\n\n## Current\nDoing stuff\n\n## History\n- old entry\n"
        result = migrate_legacy_state(legacy)

        assert isinstance(result, StateUpdateResult)
        assert result.current_content == legacy
        assert result.history_entry is None

    def test_preserves_content_exactly(self):
        legacy = "Some arbitrary content\nwith multiple lines\n"
        result = migrate_legacy_state(legacy)
        assert result.current_content == legacy


class TestAssembleStateForContext:
    def test_without_history(self):
        result = assemble_state_for_context("current stuff", "old stuff", include_history=False)
        assert result == "current stuff"

    def test_with_history(self):
        result = assemble_state_for_context("current stuff", "old stuff", include_history=True)
        assert "current stuff" in result
        assert "# State History" in result
        assert "old stuff" in result

    def test_history_only_current_exists(self):
        result = assemble_state_for_context("current", None, include_history=True)
        assert result == "current"

    def test_history_only_history_exists(self):
        result = assemble_state_for_context(None, "old stuff", include_history=True)
        assert result == "old stuff"

    def test_both_none(self):
        result = assemble_state_for_context(None, None, include_history=False)
        assert result is None

    def test_both_none_with_history(self):
        result = assemble_state_for_context(None, None, include_history=True)
        assert result is None

    def test_default_include_history_is_false(self):
        result = assemble_state_for_context("current", "old")
        assert result == "current"
