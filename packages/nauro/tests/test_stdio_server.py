"""Tests for the Nauro MCP stdio server tools."""

from pathlib import Path
from unittest.mock import patch

import pytest
from nauro_core.operations import flag_question as _flag_question_op
from nauro_core.operations.propose_decision import _get_pending_store

from nauro.mcp.stdio_server import (
    _pull_on_startup,
    _resolve_store,
    check_decision,
    confirm_decision,
    flag_question,
    get_context,
    get_raw_file,
    mcp,
    propose_decision,
    update_state,
)
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


def _append_question(store_path: Path, question: str) -> None:
    """Thin wrapper preserving the pre-cutover ``writer.append_question`` shape."""
    _flag_question_op(FilesystemStore(store_path), question, None)


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:
    """Pre-scaffolded project store with known content."""
    store_path = register_project("testproj", [tmp_path / "repo"])
    scaffold_project_store("testproj", store_path)

    (store_path / "stack.md").write_text(
        "# Stack\n- **Python 3.11** \u2014 primary language\n- **FastAPI** \u2014 HTTP framework\n"
    )
    append_decision(store_path, "Use FastAPI", rationale="Good async support for our web server.")
    _append_question(store_path, "Should we add caching?")

    return store_path


@pytest.fixture(autouse=True)
def _clear_pending():
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


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
        # Unknown name in v2 falls through to v1 legacy; if also missing
        # there, ProjectNotFoundError carries the "registry" anchor.
        from nauro.store.resolution import ProjectNotFoundError

        with pytest.raises(ProjectNotFoundError, match="registry"):
            _resolve_store("nonexistent", None)

    def test_raises_on_no_project_or_cwd(self, store: Path):
        # NoProjectError is reserved for the genuinely-no-project case.
        # Wrapper code maps only this subclass to WELCOME_NO_PROJECT.
        from nauro.store.resolution import NoProjectError

        with pytest.raises(NoProjectError, match="No Nauro project found"):
            _resolve_store(None, None)


class TestGetContext:
    def test_l0_returns_current_state(self, store: Path):
        result = get_context(project_id="testproj", level=0)
        assert "## Current State" in result["content"]

    def test_l1_returns_full_stack(self, store: Path):
        result = get_context(project_id="testproj", level=1)
        assert "# Stack" in result["content"]
        assert "Python 3.11" in result["content"]

    def test_l2_returns_full_content(self, store: Path):
        result = get_context(project_id="testproj", level=2)
        assert "Use FastAPI" in result["content"]
        assert "Should we add caching?" in result["content"]

    def test_invalid_level_rejection(self, store: Path):
        # Invalid numeric levels surface as a kernel rejection envelope —
        # `_coerce_level` rejects strings before reaching the kernel, so the
        # ValueError path on string input is exercised separately below.
        result = get_context(project_id="testproj", level=5)
        assert result["store"] == "local"
        assert result["error"]["kind"] == "rejected"
        assert "Invalid level" in result["error"]["reason"]

    def test_invalid_string_level_raises(self, store: Path):
        with pytest.raises(ValueError, match="Invalid level"):
            get_context(project_id="testproj", level="L9")


class TestProposeDecision:
    def test_propose_new_decision(self, store: Path):
        result = propose_decision(
            project_id="testproj",
            title="Use Redis for Caching",
            rationale="Fast in-memory store for session data management.",
        )
        assert result["status"] == "confirmed"
        assert "decision_id" in result

        decisions = list((store / "decisions").glob("*redis*.md"))
        assert len(decisions) >= 1

    def test_propose_rejected_empty_title(self, store: Path):
        result = propose_decision(
            project_id="testproj",
            title="",
            rationale="Some rationale text here.",
        )
        assert result["status"] == "rejected"

    def test_propose_triggers_snapshot(self, store: Path):
        propose_decision(
            project_id="testproj",
            title="Snapshot Test Decision",
            rationale="Testing that snapshots are triggered by proposals.",
        )
        snapshots = list((store / "snapshots").glob("v*.json"))
        assert len(snapshots) >= 1

    def test_skip_validation_returns_confirm_id(self, store: Path):
        result = propose_decision(
            project_id="testproj",
            title="Skip Validation Decision",
            rationale="Testing skip_validation returns a confirm_id without tier-2/tier-3.",
            skip_validation=True,
        )
        assert result["status"] == "pending_confirmation"
        assert "confirm_id" in result
        assessment = result["assessment"].lower()
        assert "skip_validation" in assessment or "skipped" in assessment

    def test_skip_validation_still_runs_tier1(self, store: Path):
        result = propose_decision(
            project_id="testproj",
            title="",
            rationale="Some rationale text here.",
            skip_validation=True,
        )
        assert result["status"] == "rejected"

    def test_skip_validation_confirm_flow(self, store: Path):
        result = propose_decision(
            project_id="testproj",
            title="Confirm After Skip",
            rationale="Testing that confirm_decision works after skip_validation.",
            skip_validation=True,
        )
        assert result["status"] == "pending_confirmation"
        cid = result["confirm_id"]

        confirmed = confirm_decision(confirm_id=cid, project_id="testproj")
        assert confirmed["status"] == "confirmed"

    def test_default_false_unchanged(self, store: Path):
        result = propose_decision(
            project_id="testproj",
            title="Default Validation Decision",
            rationale="Testing that default skip_validation=false works normally.",
        )
        # Default behavior — should go through full pipeline
        assert result["status"] in ("confirmed", "pending_confirmation")


class TestProposeDecisionResolvesQuestions:
    """propose_decision.resolves_questions wired through the stdio FastMCP layer.

    Asserts the Annotated[list[str], ...] wrapper forwards the param into
    tool_propose_decision and the resolved-questions response field is
    surfaced back to the caller.
    """

    def _seed_question(self, store: Path) -> str:
        """Write one question with a Q-form id; return its id."""
        qid = "Q1"
        (store / "open-questions.md").write_text(
            f"# Open Questions\n\n- [{qid}] should we ship the feature?\n"
        )
        return qid

    def test_known_id_moves_to_resolved(self, store: Path):
        question_id = self._seed_question(store)
        result = propose_decision(
            project_id="testproj",
            title="Ship the feature",
            rationale="Decision that closes the seeded open question.",
            resolves_questions=[question_id],
        )
        assert result["status"] == "confirmed"
        assert result.get("resolved_questions") == [question_id]
        oq = (store / "open-questions.md").read_text()
        assert "## Resolved" in oq
        assert "[Resolved by D" in oq
        assert f"[{question_id}]" in oq

    def test_unknown_id_rejects_at_boundary(self, store: Path):
        self._seed_question(store)
        result = propose_decision(
            project_id="testproj",
            title="Names a bogus id",
            rationale="Decision that names a question id that doesn't exist.",
            resolves_questions=["2099-01-01 00:00 UTC"],
        )
        assert result["status"] == "rejected"
        assessment = result["assessment"]
        assert "2099-01-01 00:00 UTC" in assessment
        assert "resolves_questions" in assessment

    def test_pending_then_confirm_applies_move(self, store: Path):
        question_id = self._seed_question(store)
        result = propose_decision(
            project_id="testproj",
            title="Closes via pending path",
            rationale="Routes through skip_validation→confirm to exercise pending storage.",
            resolves_questions=[question_id],
            skip_validation=True,
        )
        assert result["status"] == "pending_confirmation"
        cid = result["confirm_id"]
        # Question not yet moved.
        assert "## Resolved" not in (store / "open-questions.md").read_text()
        confirmed = confirm_decision(confirm_id=cid, project_id="testproj")
        assert confirmed["status"] == "confirmed"
        assert confirmed.get("resolved_questions") == [question_id]
        assert "## Resolved" in (store / "open-questions.md").read_text()


class TestWelcomeDisambiguation:
    """WELCOME_NO_PROJECT is reserved for the genuinely-no-project case.

    Specific failures (bogus project_id, missing store on disk, mismatched
    cwd config) must surface their own diagnostic instead of the onboarding
    screen suggesting `nauro init`, which is the wrong remedy when the
    caller already has a (mis-)configured project."""

    def test_unknown_project_id_surfaces_specific_error(self, store: Path):
        """Bogus project_id keyword on a dict-returning tool → guidance
        names the registry, not the welcome screen."""
        result = get_raw_file(path="project.md", project_id="does-not-exist")
        assert result["status"] == "error"
        assert "Welcome to Nauro" not in result["guidance"]
        # ProjectNotFoundError surfaces the registry-lookup framing.
        assert "registry" in result["guidance"].lower() or "not found" in result["guidance"].lower()

    def test_unknown_project_id_string_tool_surfaces_specific_error(self, store: Path):
        """Same disambiguation on a string-returning tool: a non-welcome
        message is returned for known-bogus handles."""
        result = update_state(delta="anything", project_id="does-not-exist")
        assert "Welcome to Nauro" not in result
        assert "registry" in result.lower() or "not found" in result.lower()

    def test_no_project_resolvable_still_returns_welcome(self, store: Path):
        """The genuine onboarding case — no project_id, no cwd config —
        keeps the welcome screen. This asserts the narrowing of the
        onboarding case didn't lose the legitimate trigger."""
        # Drop the registry so no project can resolve from cwd or by name.
        from nauro.store.registry import _registry_file

        _registry_file().write_text('{"projects": {}, "schema_version": 2}\n')
        result = get_raw_file(path="project.md")
        assert result["status"] == "error"
        assert "Welcome to Nauro" in result["guidance"]


class TestConfirmDecision:
    def test_confirm_invalid_id(self, store: Path):
        result = confirm_decision(
            confirm_id="nonexistent-uuid",
            project_id="testproj",
        )
        assert "error" in result


class TestCheckDecision:
    def test_check_no_conflicts(self, store: Path):
        result = check_decision(
            proposed_approach="Use a completely novel distributed tracing approach",
            project_id="testproj",
        )
        assert "related_decisions" in result
        assert "assessment" in result


class TestFlagQuestion:
    def test_records_question(self, store: Path):
        result = flag_question(project_id="testproj", question="Should we add WebSocket?")
        assert "flagged" in result.lower() or "addressed" in result.lower()

        oq = (store / "open-questions.md").read_text()
        assert "Should we add WebSocket?" in oq

    def test_includes_context(self, store: Path):
        flag_question(
            project_id="testproj",
            question="Need auth?",
            context="For the admin API",
        )
        oq = (store / "open-questions.md").read_text()
        assert "Need auth?" in oq
        assert "For the admin API" in oq


class TestUpdateState:
    def test_updates_state(self, store: Path):
        result = update_state(project_id="testproj", delta="Deployed v0.2.0")
        assert "State updated" in result

        state = (store / "state_current.md").read_text()
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


class TestToolSpecDescriptionsReachAgent:
    """Per-property descriptions in nauro_core.mcp_tools must reach agents
    via FastMCP's tools/list inputSchema, not just the parent tool
    description. Previously the local stdio's _spec_kwargs forwarded only
    title/description/annotations; FastMCP regenerated the inputSchema from
    function signatures, stripping every per-property description and enum.
    Annotated[T, Field(description=...)] + Literal[...] in the wrappers
    closes that gap."""

    @pytest.fixture
    def tools_by_name(self):
        return {t.name: t for t in mcp._tool_manager.list_tools()}

    def test_propose_decision_operation_carries_metadata_rejection_list(self, tools_by_name):
        op = tools_by_name["propose_decision"].parameters["properties"]["operation"]
        # Description should carry the canonical fragment text — the 6-field
        # metadata-rejection list, the operation enumeration, and the
        # use-supersede guidance.
        for needle in (
            "server rejects",
            "rationale-only",
            "`title`",
            "`confidence`",
            "`decision_type`",
            "`reversibility`",
            "`files_affected`",
            "`rejected`",
            "supersede",
        ):
            assert needle in op["description"], f"missing {needle!r} in operation description"
        assert op["enum"] == ["add", "update", "supersede"]

    @pytest.mark.parametrize(
        "tool,param,expected_substring",
        [
            ("propose_decision", "title", "title"),
            ("propose_decision", "rationale", "Why this decision"),
            ("propose_decision", "affected_decision_id", "decision-042"),
            ("propose_decision", "rejected", "Alternatives"),
            ("propose_decision", "confidence", "confidence"),
            ("propose_decision", "decision_type", "category"),
            ("propose_decision", "reversibility", "reverse"),
            ("propose_decision", "files_affected", "paths"),
            ("propose_decision", "skip_validation", "Tier 2"),
            ("check_decision", "proposed_approach", "approach"),
            ("check_decision", "context", "context"),
            ("get_context", "level", "L0"),
            ("get_decision", "number", "Decision number"),
            ("list_decisions", "limit", "Maximum"),
            ("list_decisions", "include_superseded", "superseded"),
            ("search_decisions", "query", "Search text"),
            ("flag_question", "question", "question"),
            ("update_state", "delta", "Description of what changed"),
            ("confirm_decision", "confirm_id", "confirm_id"),
        ],
    )
    def test_per_property_descriptions_reach_agent(
        self, tools_by_name, tool, param, expected_substring
    ):
        params = tools_by_name[tool].parameters["properties"]
        assert param in params, f"{tool} missing param {param!r}"
        desc = params[param].get("description", "")
        assert desc, f"{tool}.{param} has no description in inputSchema"
        assert expected_substring.lower() in desc.lower(), (
            f"{tool}.{param} description missing expected substring "
            f"{expected_substring!r}; got: {desc[:120]!r}"
        )

    def test_enum_constraints_present_for_propose_decision(self, tools_by_name):
        """confidence / decision_type / reversibility have no description in
        the ToolSpec, but their enum constraints must still surface so agents
        can't pass invalid values without a Pydantic validation error."""
        params = tools_by_name["propose_decision"].parameters["properties"]
        assert {"high", "medium", "low"} == set(params["confidence"]["anyOf"][0]["enum"])
        decision_type_enum = params["decision_type"]["anyOf"][0]["enum"]
        for dt in (
            "architecture",
            "library_choice",
            "pattern",
            "refactor",
            "api_design",
            "infrastructure",
            "data_model",
        ):
            assert dt in decision_type_enum
        assert {"easy", "moderate", "hard"} == set(params["reversibility"]["anyOf"][0]["enum"])

    def test_eleven_tools_registered(self):
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == 11

    @pytest.mark.parametrize(
        "tool",
        [
            "get_context",
            "get_raw_file",
            "list_decisions",
            "get_decision",
            "diff_since_last_session",
            "search_decisions",
            "check_decision",
            "propose_decision",
            "confirm_decision",
            "flag_question",
            "update_state",
        ],
    )
    def test_project_id_property_alignment_with_toolspec(self, tools_by_name, tool):
        """The local stdio's tools/list must advertise `project_id` as the
        property name on every tool that takes a project handle — matching
        the central ToolSpec (and the remote MCP). Before the rename the
        wrappers exposed `project`, which produced cross-transport schema
        drift: an agent inspecting tools/list saw different property names
        depending on which transport they connected through."""
        params = tools_by_name[tool].parameters["properties"]
        assert "project_id" in params, f"{tool} missing project_id in inputSchema"
        assert "project" not in params, (
            f"{tool} still advertises bare 'project' — should be renamed to project_id"
        )


class TestContentSizeLimits:
    """H3 STRIDE fix: local tools must reject oversized inputs."""

    def test_propose_title_at_limit(self, store: Path):
        from nauro.mcp.tools import MAX_TITLE_LENGTH

        title = "A" * MAX_TITLE_LENGTH
        result = propose_decision(
            project_id="testproj",
            title=title,
            rationale="Valid rationale that meets the minimum length requirement.",
        )
        # Should not be rejected for size
        assert result.get("status") != "rejected" or "length" not in result.get("reason", "")

    def test_propose_title_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_TITLE_LENGTH

        title = "A" * (MAX_TITLE_LENGTH + 1)
        result = propose_decision(
            project_id="testproj",
            title=title,
            rationale="Valid rationale that meets the minimum length requirement.",
        )
        assert result["status"] == "rejected"
        assert f"{MAX_TITLE_LENGTH}" in result["reason"]

    def test_propose_rationale_over_limit(self, store: Path):
        from nauro.mcp.tools import MAX_RATIONALE_LENGTH

        result = propose_decision(
            project_id="testproj",
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
        from nauro_core.constants import MAX_APPROACH_LENGTH

        from nauro.mcp.tools import tool_check_decision

        result = tool_check_decision(store, "A" * (MAX_APPROACH_LENGTH + 1))
        # Rejection envelope: structured error, related_decisions stay empty.
        assert result["related_decisions"] == []
        assert result["assessment"] == ""
        assert result["error"]["kind"] == "rejected"
        assert f"{MAX_APPROACH_LENGTH}" in result["error"]["reason"]


class TestPullOnStartup:
    """Auth and cloud-mode gating moved into hooks.py; stdio_server only
    resolves the project from cwd and delegates. These tests cover the
    resolution flow — silent-no-op-when-not-authenticated and
    silent-no-op-for-non-cloud are exercised in test_sync/test_hooks.py."""

    def test_calls_pull_when_project_resolves(self, store: Path, monkeypatch, tmp_path):
        """pull_before_session is invoked unconditionally when a project resolves."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        monkeypatch.chdir(repo_dir)

        with patch("nauro.sync.hooks.pull_before_session", return_value=3) as mock_pull:
            _pull_on_startup()
            mock_pull.assert_called_once_with("testproj", store)

    def test_does_not_raise_on_pull_failure(self, store: Path, monkeypatch, tmp_path):
        """Server startup continues even if hooks throws."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(exist_ok=True)
        monkeypatch.chdir(repo_dir)

        with patch(
            "nauro.sync.hooks.pull_before_session",
            side_effect=ConnectionError("remote unreachable"),
        ):
            _pull_on_startup()  # must not raise

    def test_skips_when_no_project_in_cwd(self, store: Path, monkeypatch, tmp_path):
        """No pull attempt when cwd maps to no registered project."""
        unrelated_dir = tmp_path / "unrelated"
        unrelated_dir.mkdir()
        monkeypatch.chdir(unrelated_dir)

        with patch("nauro.sync.hooks.pull_before_session") as mock_pull:
            _pull_on_startup()
            mock_pull.assert_not_called()
