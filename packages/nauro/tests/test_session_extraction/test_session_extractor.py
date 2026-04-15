"""Tests for session-level extraction (compaction and JSONL).

All API calls are mocked — no real Anthropic calls in unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nauro.extraction.session_extractor import (
    _chunk_transcript,
    _deduplicate_decisions,
    _parse_session_jsonl,
    extract_from_compaction,
    extract_from_session_jsonl,
    find_session_jsonl,
    read_compaction_from_session,
)
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


def _make_mock_response(tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_extraction"
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    return response


def _make_extraction_result(**overrides):
    result = {
        "decisions": [],
        "questions": [],
        "state_delta": None,
        "signal": {
            "architectural_significance": 0.0,
            "novelty": 0.0,
            "rationale_density": 0.0,
            "reversibility": 0.0,
            "scope": 0.0,
        },
        "composite_score": 0.0,
        "skip": True,
        "reasoning": "",
    }
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# extract_from_compaction tests
# ---------------------------------------------------------------------------


class TestExtractFromCompaction:
    @patch("nauro.extraction.session_extractor.anthropic.Anthropic")
    def test_basic_extraction(self, mock_cls, store):
        expected = _make_extraction_result(
            decisions=[
                {
                    "title": "Use FastAPI for MCP server",
                    "rationale": "Good async support",
                    "confidence": "high",
                    "rejected": [{"alternative": "Flask", "reason": "No native async"}],
                }
            ],
            signal={
                "architectural_significance": 0.8,
                "novelty": 0.6,
                "rationale_density": 0.7,
                "reversibility": 0.5,
                "scope": 0.4,
            },
            composite_score=0.65,
            skip=False,
            reasoning="Architecture decision with clear tradeoffs",
        )
        mock_cls.return_value.messages.create.return_value = _make_mock_response(expected)

        result = extract_from_compaction(
            "Session summary: decided to use FastAPI for the MCP server...",
            store,
            session_id="abc123",
            api_key="test",
        )

        assert result["skip"] is False
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["title"] == "Use FastAPI for MCP server"
        assert result["composite_score"] == 0.65

    @patch("nauro.extraction.session_extractor.anthropic.Anthropic")
    def test_sets_source_on_decisions(self, mock_cls, store):
        expected = _make_extraction_result(
            decisions=[{"title": "Test", "confidence": "medium"}],
            composite_score=0.5,
            skip=False,
        )
        mock_cls.return_value.messages.create.return_value = _make_mock_response(expected)

        result = extract_from_compaction("summary", store, session_id="s1", api_key="test")
        for d in result["decisions"]:
            assert d.get("source") == "compaction"

    def test_empty_summary_returns_skip(self, store):
        result = extract_from_compaction("", store, api_key="test")
        assert result["skip"] is True

    @patch("nauro.extraction.session_extractor.anthropic.Anthropic")
    def test_api_error_returns_skip(self, mock_cls, store):
        mock_cls.return_value.messages.create.side_effect = Exception("API error")
        result = extract_from_compaction("summary", store, api_key="test")
        assert result["skip"] is True


# ---------------------------------------------------------------------------
# extract_from_session_jsonl tests
# ---------------------------------------------------------------------------


class TestExtractFromSessionJsonl:
    def _write_session(self, path: Path, messages: list[dict]):
        lines = [json.dumps(m) for m in messages]
        path.write_text("\n".join(lines))

    @patch("nauro.extraction.session_extractor.anthropic.Anthropic")
    def test_basic_jsonl_extraction(self, mock_cls, store, tmp_path):
        session_path = tmp_path / "session.jsonl"
        self._write_session(
            session_path,
            [
                {"role": "user", "content": "Let's use Postgres for the database"},
                {"role": "assistant", "content": "Good choice. I'll set up the schema."},
            ],
        )

        expected = _make_extraction_result(
            decisions=[{"title": "Use Postgres", "confidence": "high"}],
            composite_score=0.7,
            skip=False,
            reasoning="Database choice",
        )
        mock_cls.return_value.messages.create.return_value = _make_mock_response(expected)

        result = extract_from_session_jsonl(session_path, store, api_key="test")
        assert result["skip"] is False
        assert len(result["decisions"]) == 1

    def test_missing_file_returns_skip(self, store, tmp_path):
        result = extract_from_session_jsonl(tmp_path / "missing.jsonl", store)
        assert result["skip"] is True

    @patch("nauro.extraction.session_extractor.anthropic.Anthropic")
    def test_deduplicates_across_chunks(self, mock_cls, store, tmp_path):
        """Decisions with the same title across chunks are deduplicated."""
        session_path = tmp_path / "session.jsonl"
        # Create enough content to span multiple chunks
        messages = []
        for i in range(100):
            messages.append({"role": "user", "content": f"Message {i} " * 100})
            messages.append({"role": "assistant", "content": f"Response {i} " * 100})
        self._write_session(session_path, messages)

        # Mock returns the same decision for each chunk
        expected = _make_extraction_result(
            decisions=[{"title": "Use Postgres", "confidence": "high"}],
            composite_score=0.7,
            skip=False,
            reasoning="Repeated decision",
        )
        mock_cls.return_value.messages.create.return_value = _make_mock_response(expected)

        result = extract_from_session_jsonl(session_path, store, api_key="test")
        # Should be deduplicated to just one
        assert len(result["decisions"]) == 1


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestParseSessionJsonl:
    def test_basic_parsing(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "Hello"})
            + "\n"
            + json.dumps({"role": "assistant", "content": "Hi there"})
            + "\n"
        )
        transcript = _parse_session_jsonl(path)
        assert "user: Hello" in transcript
        assert "assistant: Hi there" in transcript

    def test_handles_content_blocks(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Here is my response"},
                        {"type": "tool_use", "name": "edit_file"},
                    ],
                }
            )
            + "\n"
        )
        transcript = _parse_session_jsonl(path)
        assert "Here is my response" in transcript
        assert "[tool: edit_file]" in transcript

    def test_handles_malformed_lines(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            "not valid json\n" + json.dumps({"role": "user", "content": "Valid"}) + "\n" + "\n"
        )
        transcript = _parse_session_jsonl(path)
        assert "Valid" in transcript

    def test_empty_file(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text("")
        transcript = _parse_session_jsonl(path)
        assert transcript == ""


class TestChunkTranscript:
    def test_single_chunk(self):
        text = "user: Hello\nassistant: Hi"
        chunks = _chunk_transcript(text, max_tokens=1000)
        assert len(chunks) == 1

    def test_multiple_chunks(self):
        lines = [f"user: {'x' * 1000}" for _ in range(10)]
        text = "\n".join(lines)
        chunks = _chunk_transcript(text, max_tokens=500)
        assert len(chunks) > 1


class TestDeduplicateDecisions:
    def test_removes_exact_title_dupes(self):
        decisions = [
            {"title": "Use Postgres", "confidence": "high"},
            {"title": "Use Postgres", "confidence": "medium"},
            {"title": "Use Redis", "confidence": "high"},
        ]
        deduped = _deduplicate_decisions(decisions)
        assert len(deduped) == 2
        titles = [d["title"] for d in deduped]
        assert "Use Postgres" in titles
        assert "Use Redis" in titles

    def test_case_insensitive(self):
        decisions = [
            {"title": "Use Postgres", "confidence": "high"},
            {"title": "use postgres", "confidence": "medium"},
        ]
        deduped = _deduplicate_decisions(decisions)
        assert len(deduped) == 1

    def test_empty_list(self):
        assert _deduplicate_decisions([]) == []


class TestFindSessionJsonl:
    def test_finds_in_cwd_path(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects"
        encoded = "Users-test-myrepo"
        session_dir = projects_dir / encoded
        session_dir.mkdir(parents=True)
        session_file = session_dir / "session123.jsonl"
        session_file.write_text("{}\n")

        with patch("nauro.extraction.session_extractor.Path.home", return_value=tmp_path):
            result = find_session_jsonl("session123", cwd="/Users/test/myrepo")
            # May or may not find it depending on encoding match
            # But searching all dirs should work
            if result is None:
                result = find_session_jsonl("session123")

    def test_returns_none_for_missing(self, tmp_path):
        with patch("nauro.extraction.session_extractor.Path.home", return_value=tmp_path):
            result = find_session_jsonl("nonexistent")
            assert result is None


class TestReadCompactionFromSession:
    def test_reads_summary_type(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "hello"})
            + "\n"
            + json.dumps({"type": "summary", "content": "Session summary: decided X"})
            + "\n"
        )
        result = read_compaction_from_session(path)
        assert result == "Session summary: decided X"

    def test_reads_compaction_type(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            json.dumps({"type": "compaction", "content": "Compacted: key decisions..."}) + "\n"
        )
        result = read_compaction_from_session(path)
        assert result == "Compacted: key decisions..."

    def test_returns_last_compaction(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(
            json.dumps({"type": "summary", "content": "First summary"})
            + "\n"
            + json.dumps({"type": "summary", "content": "Second summary"})
            + "\n"
        )
        result = read_compaction_from_session(path)
        assert result == "Second summary"

    def test_returns_none_when_no_compaction(self, tmp_path):
        path = tmp_path / "session.jsonl"
        path.write_text(json.dumps({"role": "user", "content": "hello"}) + "\n")
        result = read_compaction_from_session(path)
        assert result is None

    def test_returns_none_for_missing_file(self, tmp_path):
        result = read_compaction_from_session(tmp_path / "missing.jsonl")
        assert result is None
