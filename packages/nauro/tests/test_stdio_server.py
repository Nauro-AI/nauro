"""Tests for the Nauro MCP stdio server tools."""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.mcp.stdio_server import (
    _pull_on_startup,
    _resolve_store,
    check_decision,
    confirm_decision,
    flag_question,
    get_context,
    mcp,
    propose_decision,
    update_state,
)
from nauro.store.registry import register_project
from nauro.store.writer import append_decision, append_question
from nauro.templates.scaffolds import scaffold_project_store
from nauro.validation.pending import clear_all


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Pre-scaffolded project store with known content."""
    monkeypatch.setenv("NAURO_HOME", str(tmp_path))
    store_path = register_project("testproj", [tmp_path / "repo"])
    scaffold_project_store("testproj", store_path)

    (store_path / "stack.md").write_text(
        "# Stack\n- Python 3.11 — primary language\n- FastAPI — HTTP framework\n"
    )
    append_decision(store_path, "Use FastAPI", rationale="Good async support for our web server.")
    append_question(store_path, "Should we add caching?")

    return store_path


@pytest.fixture(autouse=True)
def _clear_pending():
    clear_all()
    yield
    clear_all()


class TestResolveStore:
    def test_resolve_by_project_name(self, store: Path):
        result = _resolve_store("testproj", None)
        assert result == store

    def test_resolve_by_cwd(self, store: Path, tmp_path: Path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        result = _resolve_store(None, str(repo_dir))
        assert result == store

    def test_raises_on_unknown_project(self, store: Path):
        with pytest.raises(ValueError, match="Project store not found"):
            _resolve_store("nonexistent", None)

    def test_raises_on_no_project_or_cwd(self, store: Path):
        with pytest.raises(ValueError, match="Could not resolve project"):
            _resolve_store(None, None)


class TestGetContext:
    def test_l0_returns_state(self, store: Path):
        result = get_context(project="testproj", level=0)
        assert "Current State" in result

    def test_l1_returns_full_stack(self, store: Path):
        result = get_context(project="testproj", level=1)
        assert "# Stack" in result
        assert "Python 3.11" in result

    def test_l2_returns_full_content(self, store: Path):
        result = get_context(project="testproj", level=2)
        assert "Use FastAPI" in result
        assert "Should we add caching?" in result

    def test_invalid_level_raises(self, store: Path):
        with pytest.raises(ValueError, match="Invalid level"):
            get_context(project="testproj", level=5)


class TestProposeDecision:
    def test_propose_new_decision(self, store: Path):
        result = propose_decision(
            project="testproj",
            title="Use Redis for Caching",
            rationale="Fast in-memory store for session data management.",
        )
        assert result["status"] == "confirmed"
        assert "decision_id" in result

        decisions = list((store / "decisions").glob("*redis*.md"))
        assert len(decisions) >= 1

    def test_propose_rejected_empty_title(self, store: Path):
        result = propose_decision(
            project="testproj",
            title="",
            rationale="Some rationale text here.",
        )
        assert result["status"] == "rejected"

    def test_propose_triggers_snapshot(self, store: Path):
        propose_decision(
            project="testproj",
            title="Snapshot Test Decision",
            rationale="Testing that snapshots are triggered by proposals.",
        )
        snapshots = list((store / "snapshots").glob("v*.json"))
        assert len(snapshots) >= 1

    def test_skip_validation_returns_confirm_id(self, store: Path):
        result = propose_decision(
            project="testproj",
            title="Skip Validation Decision",
            rationale="Testing skip_validation returns a confirm_id without tier-2/tier-3.",
            skip_validation=True,
        )
        assert result["status"] == "pending_confirmation"
        assert "confirm_id" in result
        assessment = result["validation"]["assessment"].lower()
        assert "skip_validation" in assessment or "skipped" in assessment

    def test_skip_validation_still_runs_tier1(self, store: Path):
        result = propose_decision(
            project="testproj",
            title="",
            rationale="Some rationale text here.",
            skip_validation=True,
        )
        assert result["status"] == "rejected"

    def test_skip_validation_confirm_flow(self, store: Path):
        result = propose_decision(
            project="testproj",
            title="Confirm After Skip",
            rationale="Testing that confirm_decision works after skip_validation.",
            skip_validation=True,
        )
        assert result["status"] == "pending_confirmation"
        cid = result["confirm_id"]

        confirmed = confirm_decision(confirm_id=cid, project="testproj")
        assert confirmed["status"] == "confirmed"

    def test_default_false_unchanged(self, store: Path):
        result = propose_decision(
            project="testproj",
            title="Default Validation Decision",
            rationale="Testing that default skip_validation=false works normally.",
        )
        # Default behavior — should go through full pipeline
        assert result["status"] in ("confirmed", "pending_confirmation")


class TestConfirmDecision:
    def test_confirm_invalid_id(self, store: Path):
        result = confirm_decision(
            confirm_id="nonexistent-uuid",
            project="testproj",
        )
        assert "error" in result


class TestCheckDecision:
    def test_check_no_conflicts(self, store: Path):
        result = check_decision(
            proposed_approach="Use a completely novel distributed tracing approach",
            project="testproj",
        )
        assert "related_decisions" in result
        assert "assessment" in result


class TestFlagQuestion:
    def test_records_question(self, store: Path):
        result = flag_question(project="testproj", question="Should we add WebSocket?")
        assert "flagged" in result.lower() or "addressed" in result.lower()

        oq = (store / "open-questions.md").read_text()
        assert "Should we add WebSocket?" in oq

    def test_includes_context(self, store: Path):
        flag_question(
            project="testproj",
            question="Need auth?",
            context="For the admin API",
        )
        oq = (store / "open-questions.md").read_text()
        assert "Need auth?" in oq
        assert "For the admin API" in oq


class TestUpdateState:
    def test_updates_state(self, store: Path):
        result = update_state(project="testproj", delta="Deployed v0.2.0")
        assert "State updated" in result

        state = (store / "state.md").read_text()
        assert "Deployed v0.2.0" in state


class TestToolRegistration:
    def test_tools_are_registered(self):
        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        assert "get_context" in tool_names
        assert "propose_decision" in tool_names
        assert "confirm_decision" in tool_names
        assert "check_decision" in tool_names
        assert "flag_question" in tool_names
        assert "update_state" in tool_names
        assert "search_decisions" in tool_names
        assert "get_raw_file" in tool_names
        assert "list_decisions" in tool_names
        assert "get_decision" in tool_names
        assert "diff_since_last_session" in tool_names

    def test_eleven_tools_registered(self):
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == 11


class TestContentSizeLimits:
    """H3 STRIDE fix: local tools must reject oversized inputs."""

    def test_propose_title_at_limit(self, store: Path):
        from nauro.mcp.tools import MAX_TITLE_LENGTH

        title = "A" * MAX_TITLE_LENGTH
        result = propose_decision(
            project="testproj",
            title=title,
            rationale="Valid rationale that meets the minimum length requirement.",
        )
        # Should not be rejected for size
        assert result.get("status") != "rejected" or "length" not in result.get("reason", "")

    def test_propose_title_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_TITLE_LENGTH

        title = "A" * (MAX_TITLE_LENGTH + 1)
        result = propose_decision(
            project="testproj",
            title=title,
            rationale="Valid rationale that meets the minimum length requirement.",
        )
        assert result["status"] == "rejected"
        assert f"{MAX_TITLE_LENGTH}" in result["reason"]

    def test_propose_rationale_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_RATIONALE_LENGTH

        result = propose_decision(
            project="testproj",
            title="Valid title",
            rationale="X" * (MAX_RATIONALE_LENGTH + 1),
        )
        assert result["status"] == "rejected"
        assert f"{MAX_RATIONALE_LENGTH}" in result["reason"]

    def test_flag_question_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_QUESTION_LENGTH, tool_flag_question

        result = tool_flag_question(store, "Q" * (MAX_QUESTION_LENGTH + 1))
        assert result["status"] == "rejected"
        assert f"{MAX_QUESTION_LENGTH}" in result["reason"]

    def test_update_state_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_DELTA_LENGTH, tool_update_state

        result = tool_update_state(store, "D" * (MAX_DELTA_LENGTH + 1))
        assert result["status"] == "rejected"
        assert f"{MAX_DELTA_LENGTH}" in result["reason"]

    def test_check_decision_approach_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_APPROACH_LENGTH, tool_check_decision

        result = tool_check_decision(store, "A" * (MAX_APPROACH_LENGTH + 1))
        assert result["status"] == "rejected"
        assert f"{MAX_APPROACH_LENGTH}" in result["reason"]


class TestPullOnStartup:
    def test_skips_when_sync_not_configured(self, store: Path, monkeypatch):
        """No pull attempt when sync credentials are absent."""
        monkeypatch.chdir(store.parent)

        disabled_config = type("SyncConfig", (), {"enabled": False})()
        with patch("nauro.sync.config.load_sync_config", return_value=disabled_config):
            with patch("nauro.sync.hooks.pull_before_session") as mock_pull:
                _pull_on_startup()
                mock_pull.assert_not_called()

    def test_calls_pull_when_sync_configured(self, store: Path, monkeypatch, tmp_path):
        """pull_before_session is called when sync is enabled and project is found."""
        # cwd must resolve to our registered project
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        monkeypatch.chdir(repo_dir)

        enabled_config = type(
            "SyncConfig",
            (),
            {
                "enabled": True,
                "bucket_name": "test-bucket",
                "region": "eu-north-1",
                "access_key_id": "key",
                "secret_access_key": "secret",
            },
        )()

        with patch("nauro.sync.config.load_sync_config", return_value=enabled_config):
            with patch("nauro.sync.hooks.pull_before_session", return_value=3) as mock_pull:
                _pull_on_startup()
                mock_pull.assert_called_once_with("testproj", store)

    def test_does_not_raise_on_pull_failure(self, store: Path, monkeypatch, tmp_path):
        """Server startup continues even if pull throws."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        monkeypatch.chdir(repo_dir)

        enabled_config = type(
            "SyncConfig",
            (),
            {
                "enabled": True,
                "bucket_name": "test-bucket",
                "region": "eu-north-1",
                "access_key_id": "key",
                "secret_access_key": "secret",
            },
        )()

        with patch("nauro.sync.config.load_sync_config", return_value=enabled_config):
            with patch(
                "nauro.sync.hooks.pull_before_session",
                side_effect=ConnectionError("S3 unreachable"),
            ):
                # Must not raise
                _pull_on_startup()

    def test_skips_when_no_project_in_cwd(self, store: Path, monkeypatch, tmp_path):
        """No pull attempt when cwd maps to no registered project."""
        unrelated_dir = tmp_path / "unrelated"
        unrelated_dir.mkdir()
        monkeypatch.chdir(unrelated_dir)

        enabled_config = type(
            "SyncConfig",
            (),
            {
                "enabled": True,
                "bucket_name": "test-bucket",
                "region": "eu-north-1",
                "access_key_id": "key",
                "secret_access_key": "secret",
            },
        )()

        with patch("nauro.sync.config.load_sync_config", return_value=enabled_config):
            with patch("nauro.sync.hooks.pull_before_session") as mock_pull:
                _pull_on_startup()
                mock_pull.assert_not_called()
