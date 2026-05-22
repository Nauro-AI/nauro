"""Rejection of tool-use envelope fragments at the writer boundary.

Some non-Anthropic agent surfaces emit MCP tool calls as XML and their
bridges occasionally fail to extract <parameter> values cleanly, so the
envelope tail leaks into the string field the server receives. The local
MCP tools must reject before any I/O — see
nauro_core.validation.find_envelope_token.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.mcp.tools import tool_flag_question, tool_propose_decision
from nauro.store.registry import register_project
from nauro.templates.scaffolds import scaffold_project_store

OPEN_QUESTIONS = "open-questions.md"


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """A scaffolded project store registered against tmp_path."""
    store_path = register_project("testproj", [tmp_path])
    scaffold_project_store("testproj", store_path)
    return store_path


class TestFlagQuestionRejectsEnvelope:
    def test_question_with_closing_question_tag_is_rejected(self, store: Path):
        oq_path = store / OPEN_QUESTIONS
        before = oq_path.read_text()

        result = tool_flag_question(
            store,
            question="Should we adopt OpenTelemetry?</question>",
        )

        assert result["status"] == "rejected"
        assert "question contains tool-use envelope fragment" in result["reason"]
        assert "</question>" in result["reason"]
        # File must not have been mutated.
        assert oq_path.read_text() == before

    def test_context_with_opening_parameter_attr_is_rejected(self, store: Path):
        oq_path = store / OPEN_QUESTIONS
        before = oq_path.read_text()

        result = tool_flag_question(
            store,
            question="Should we adopt OpenTelemetry?",
            context='leaked envelope <parameter name="rationale">',
        )

        assert result["status"] == "rejected"
        assert "context contains tool-use envelope fragment" in result["reason"]
        assert oq_path.read_text() == before

    def test_clean_inputs_still_write(self, store: Path):
        with patch("nauro.mcp.tools._try_push"):
            result = tool_flag_question(
                store,
                question="Should we adopt OpenTelemetry for tracing?",
                context="Affects the gateway and three downstream services.",
            )
        assert result["status"] == "ok"
        content = (store / OPEN_QUESTIONS).read_text()
        assert "Should we adopt OpenTelemetry for tracing?" in content


class TestProposeDecisionRejectsEnvelope:
    def test_rationale_with_closing_rationale_tag_is_rejected(self, store: Path):
        result = tool_propose_decision(
            store,
            title="Use Postgres for primary storage",
            rationale=(
                "ACID guarantees and strong tooling beat the operational cost. </rationale>"
            ),
        )
        assert result["status"] == "rejected"
        assert "rationale contains tool-use envelope fragment" in result["reason"]
        assert "</rationale>" in result["reason"]

    def test_title_with_envelope_token_is_rejected(self, store: Path):
        result = tool_propose_decision(
            store,
            title="Use Postgres</invoke>",
            rationale="ACID guarantees and strong tooling beat operational cost.",
        )
        assert result["status"] == "rejected"
        assert "title contains tool-use envelope fragment" in result["reason"]

    def test_rejected_alternative_reason_envelope_is_rejected(self, store: Path):
        result = tool_propose_decision(
            store,
            title="Use Postgres for primary storage",
            rationale="ACID guarantees and strong tooling beat operational cost.",
            rejected=[
                {
                    "alternative": "MySQL",
                    "reason": "Weaker isolation defaults.</parameter>",
                }
            ],
        )
        assert result["status"] == "rejected"
        assert "rejected[0].reason contains tool-use envelope fragment" in result["reason"]

    def test_clean_inputs_still_validate(self, store: Path):
        with patch("nauro.mcp.tools._try_push"):
            result = tool_propose_decision(
                store,
                title="Use Postgres for primary storage",
                rationale="ACID guarantees and strong tooling beat operational cost.",
            )
        assert result["status"] == "confirmed"
