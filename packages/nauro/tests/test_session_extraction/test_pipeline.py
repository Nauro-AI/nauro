"""Tests for extraction pipeline updates — signal format and extraction log."""

from __future__ import annotations

import json

from nauro.extraction.pipeline import _append_extraction_log, _make_skip_result


class TestSkipResult:
    def test_has_all_required_keys(self):
        result = _make_skip_result()
        assert result["decisions"] == []
        assert result["questions"] == []
        assert result["state_delta"] is None
        assert result["skip"] is True
        assert result["composite_score"] == 0.0
        assert "signal" in result
        assert result["signal"]["architectural_significance"] == 0.0
        assert result["reasoning"] == ""


class TestExtractionLog:
    def test_appends_jsonl(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        _append_extraction_log(store, {"key": "value1"})
        _append_extraction_log(store, {"key": "value2"})

        log_path = store / "extraction-log.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["key"] == "value1"
        assert "timestamp" in entry1

        entry2 = json.loads(lines[1])
        assert entry2["key"] == "value2"

    def test_never_crashes(self, tmp_path):
        # Path that doesn't exist — should not raise
        _append_extraction_log(tmp_path / "nonexistent" / "store", {"test": True})

    def test_includes_timestamp(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        _append_extraction_log(store, {"event": "test"})

        log_path = store / "extraction-log.jsonl"
        entry = json.loads(log_path.read_text().strip())
        assert "timestamp" in entry
        assert "T" in entry["timestamp"]  # ISO format
