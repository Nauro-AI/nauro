"""Tier 3 validation — LLM evaluation.

Only called for ~5% of writes where Tier 2 detected high similarity.
Uses Haiku for classification.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import anthropic
except ImportError:
    raise ImportError("anthropic package required for validation: pip install nauro[extraction]")

from nauro.constants import DEFAULT_EXTRACTION_MODEL, NAURO_EXTRACTION_MODEL_ENV
from nauro.store.reader import _list_decisions

logger = logging.getLogger("nauro.validation.tier3")

EVALUATION_TOOL = {
    "name": "evaluate_decision",
    "description": (
        "Evaluate whether a proposed decision should be added,"
        " should update or supersede an existing decision,"
        " or is redundant."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["add", "update", "supersede", "noop"],
                "description": (
                    "add=new decision, update=augment existing,"
                    " supersede=replace existing, noop=redundant"
                ),
            },
            "assessment": {
                "type": "string",
                "description": "Brief explanation of the determination.",
            },
            "suggested_refinements": {
                "type": ["string", "null"],
                "description": "Optional suggestion for improving the proposal title or rationale.",
            },
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "string"},
                        "conflict_description": {"type": "string"},
                    },
                    "required": ["decision_id", "conflict_description"],
                },
                "description": "Decisions that conflict with the proposal.",
            },
            "affected_decision_id": {
                "type": ["string", "null"],
                "description": "For update/supersede: the decision_id to update or supersede.",
            },
        },
        "required": ["operation", "assessment", "conflicts"],
    },
}

CONFLICT_CHECK_TOOL = {
    "name": "check_conflicts",
    "description": "Check whether a proposed approach conflicts with existing decisions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "related_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "string"},
                        "relevance": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["decision_id", "relevance"],
                },
            },
            "potential_conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "decision_id": {"type": "string"},
                        "conflict": {"type": "string"},
                    },
                    "required": ["decision_id", "conflict"],
                },
            },
            "assessment": {
                "type": "string",
                "description": (
                    "Overall assessment of how the approach relates to existing decisions."
                ),
            },
        },
        "required": ["related_decisions", "potential_conflicts", "assessment"],
    },
}

EVALUATION_SYSTEM = (
    "You are evaluating whether a proposed architectural decision should be added "
    "to a project's decision store. You are given the proposed decision and the most "
    "similar existing decisions. Determine the correct operation.\n\n"
    "Guidelines:\n"
    "- ADD: The proposal is meaningfully different from all similar"
    " decisions. It covers new ground.\n"
    "- UPDATE: The proposal adds information to an existing decision"
    " but doesn't change the conclusion. The existing decision"
    " should be augmented. Return the decision_id to update"
    " in affected_decision_id.\n"
    "- SUPERSEDE: The proposal replaces or reverses an existing"
    " decision. The old decision should be marked superseded."
    " Return the decision_id being superseded"
    " in affected_decision_id.\n"
    "- NOOP: The proposal is redundant — it's already captured"
    " by an existing decision. Don't write anything."
)

CONFLICT_CHECK_SYSTEM = (
    "You are checking whether a proposed approach conflicts with"
    " existing project decisions. Analyze the proposal against the"
    " existing decisions and identify any conflicts or relevant"
    " context."
)


def evaluate_with_llm(
    proposal: dict,
    similar_decisions: list[dict],
    project_path: Path,
    api_key: str | None = None,
) -> dict:
    """Evaluate a proposal against similar decisions using Haiku.

    Args:
        proposal: The proposed decision dict.
        similar_decisions: Decisions flagged as similar by Tier 2.
        project_path: Path to the project store.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        {operation, assessment, suggested_refinements, conflicts, affected_decision_id}
        operation is "hold" when the LLM is unavailable (fail-closed).
    """
    # Load full content of similar decisions
    all_decisions = _list_decisions(project_path)
    decision_map = {}
    for d in all_decisions:
        decision_id = f"decision-{d['num']:03d}"
        decision_map[decision_id] = d

    similar_content = []
    for sim in similar_decisions:
        sim_d = decision_map.get(sim["id"])
        if sim_d:
            similar_content.append(
                f"### {sim['id']}: {sim_d['title']} (similarity: {sim['similarity']})\n"
                f"{sim_d['content']}"
            )

    user_prompt = (
        "## Proposed Decision\n\n"
        f"**Title:** {proposal.get('title', '')}\n"
        f"**Rationale:** {proposal.get('rationale', '')}\n"
        f"**Confidence:** {proposal.get('confidence', 'medium')}\n"
        f"**Type:** {proposal.get('decision_type', 'unknown')}\n\n"
        "## Most Similar Existing Decisions\n\n" + "\n\n---\n\n".join(similar_content)
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get(NAURO_EXTRACTION_MODEL_ENV, DEFAULT_EXTRACTION_MODEL)

        response = client.messages.create(  # type: ignore[call-overload]
            model=model,
            max_tokens=512,
            system=EVALUATION_SYSTEM,
            tools=[EVALUATION_TOOL],
            tool_choice={"type": "tool", "name": "evaluate_decision"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "evaluate_decision":
                result = block.input
                result.setdefault("operation", "add")
                result.setdefault("assessment", "")
                result.setdefault("suggested_refinements", None)
                result.setdefault("conflicts", [])
                result.setdefault("affected_decision_id", None)
                return result  # type: ignore[no-any-return]

    except Exception as e:
        logger.warning("LLM evaluation failed — holding decision for manual review: %s", e)

    # Fail-closed: do not auto-add when LLM is unavailable.
    # The pipeline will skip (auto_confirm path) or queue for human review (MCP path).
    return {
        "operation": "hold",
        "assessment": "LLM evaluation unavailable — decision held for manual review.",
        "suggested_refinements": None,
        "conflicts": [],
        "affected_decision_id": None,
    }


def check_conflicts_with_llm(
    proposed_approach: str,
    context: str | None,
    similar_decisions: list[dict],
    project_path: Path,
    api_key: str | None = None,
) -> dict:
    """Check if a proposed approach conflicts with existing decisions.

    Args:
        proposed_approach: Description of the approach being considered.
        context: Optional additional context.
        similar_decisions: Decisions flagged as similar by Tier 2.
        project_path: Path to the project store.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        {related_decisions, potential_conflicts, assessment}
    """
    all_decisions = _list_decisions(project_path)
    decision_map = {}
    for d in all_decisions:
        decision_id = f"decision-{d['num']:03d}"
        decision_map[decision_id] = d

    similar_content = []
    for sim in similar_decisions:
        sim_d = decision_map.get(sim["id"])
        if sim_d:
            similar_content.append(f"### {sim['id']}: {sim_d['title']}\n{sim_d['content']}")

    user_prompt = f"## Proposed Approach\n\n{proposed_approach}\n\n"
    if context:
        user_prompt += f"**Context:** {context}\n\n"
    user_prompt += "## Relevant Existing Decisions\n\n" + "\n\n---\n\n".join(similar_content)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get(NAURO_EXTRACTION_MODEL_ENV, DEFAULT_EXTRACTION_MODEL)

        response = client.messages.create(  # type: ignore[call-overload]
            model=model,
            max_tokens=512,
            system=CONFLICT_CHECK_SYSTEM,
            tools=[CONFLICT_CHECK_TOOL],
            tool_choice={"type": "tool", "name": "check_conflicts"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "check_conflicts":
                result = block.input
                result.setdefault("related_decisions", [])
                result.setdefault("potential_conflicts", [])
                result.setdefault("assessment", "")
                return result  # type: ignore[no-any-return]

    except Exception as e:
        logger.warning("LLM conflict check failed: %s", e)

    return {
        "related_decisions": [],
        "potential_conflicts": [],
        "assessment": "LLM conflict check unavailable.",
    }
