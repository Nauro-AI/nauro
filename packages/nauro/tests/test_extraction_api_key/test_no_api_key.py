"""Tests for graceful degradation when API key is absent."""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.extraction.pipeline import (
    _has_api_key,
    _show_no_api_key_hint,
    extract_from_commit,
    process_commit,
)
from nauro.extraction.types import ExtractionSkipped
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture()
def project_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store)
    return store


class TestApiKeyCheck:
    def test_has_api_key_with_env_var(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert _has_api_key() is True

    def test_has_api_key_with_explicit_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _has_api_key("sk-test") is True

    def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _has_api_key() is False


class TestExtractorSkipsCleanly:
    def test_extract_from_commit_returns_no_api_key_result(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = extract_from_commit("test commit", "1 file changed", ["test.py"])
        assert isinstance(result, ExtractionSkipped)
        assert result.reason == "no_api_key"

    def test_process_commit_logs_null_composite_score(self, monkeypatch, project_store):
        """process_commit should log with null composite_score when no key."""
        import json

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Mock get_commit_info to return data without needing a real repo
        with patch(
            "nauro.extraction.pipeline.get_commit_info",
            return_value=("test commit", "1 file", ["test.py"]),
        ):
            result = process_commit(str(project_store), project_store)

        assert result is None

        # Check extraction log
        log_path = project_store / "extraction-log.jsonl"
        assert log_path.exists()
        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        last = entries[-1]
        assert last["composite_score"] is None
        assert last["reasoning"] == "no_api_key"
        assert last["skip"] is True


class TestOneTimeHint:
    def test_hint_fires_once(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        # Ensure .hints doesn't exist
        hints_file = tmp_path / ".hints"
        assert not hints_file.exists()

        _show_no_api_key_hint()
        captured = capsys.readouterr()
        assert "LLM extraction inactive" in captured.err

        # Second call should NOT print
        _show_no_api_key_hint()
        captured2 = capsys.readouterr()
        assert captured2.err == ""

        # Sentinel should be in hints file
        assert "no_api_key_hint_shown" in hints_file.read_text()

    def test_hint_does_not_fire_if_sentinel_exists(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path))
        hints_file = tmp_path / ".hints"
        hints_file.write_text("no_api_key_hint_shown\n")

        _show_no_api_key_hint()
        captured = capsys.readouterr()
        assert captured.err == ""
