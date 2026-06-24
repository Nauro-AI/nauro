"""Integration: a Step-0 Rapid-Cited-Seed card reaches check_decision retrieval.

Step 0 of the adopt skill files a documented decision through the same screened
``propose_decision`` path the rest of the skill uses, with the source citation
persisted as a free-text ``Source: file:line`` line in the rationale. This test
exercises that seam end to end: file a ``num >= 2`` decision through the local
``tool_propose_decision`` adapter (so it clears the scaffold-seed filter), then
confirm the proactive ``check_decision`` retrieval hook actually fires against
the seeded store on the 1.0.1 instruction floor — it is not aspirational prose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nauro_core import MCP_INSTRUCTIONS_STATIC

from nauro.cli.commands.import_cmd import _extract_adr_alternatives_strict
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp.tools import tool_check_decision, tool_propose_decision
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

# The Step-0 contract is "no model-composed why": every span a card carries is
# either quoted verbatim from a source doc or typed by the human. This test
# proves that end to end by deriving the seed's rejected reason from the strict
# ADR extractor (the verbatim subsection body, not a paraphrase) and using a
# rationale span quoted verbatim from the fixture's ## Decision section.
FIXTURE_ADR = Path(__file__).parent / "fixtures" / "adr" / "0003-shared-store-daemon.md"

# Verbatim span from the fixture's ## Decision section (asserted present below).
_RATIONALE_SPAN = (
    "Introduce a single store-owned local daemon as the primary shared writer and\n"
    "query service for multi-agent workflows."
)


def _extracted_keep_cli_reason() -> str:
    """The verbatim 'Keep CLI-Only Embedded Access' rejection reason, taken
    straight from the strict extractor's output on the fixture ADR — so the seed
    is the extractor's quoted span, never composed for the test."""
    entries = _extract_adr_alternatives_strict(FIXTURE_ADR.read_text(encoding="utf-8"))
    assert entries, "fixture must yield strict alternatives"
    entry = next(e for e in entries if e["alternative"] == "Keep CLI-Only Embedded Access")
    return entry["reason"]


@pytest.fixture
def seeded_store(tmp_path: Path) -> Path:
    """A scaffolded store with one Step-0-shaped cited card filed (num >= 2).

    The card is built the way Step 0.4 builds it: a rationale span quoted
    verbatim from the source ADR plus a free-text Source: citation, and a named
    rejected alternative whose reason is the strict extractor's verbatim
    subsection body — nothing composed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store_path = register_project_v2("step0-project", [repo], mode=REPO_CONFIG_MODE_LOCAL)
    scaffold_project_store("step0-project", store_path)

    result = tool_propose_decision(
        store_path,
        title="Use a store-owned daemon for shared store access",
        rationale=(_RATIONALE_SPAN + "\n\nSource: docs/adr/0003-shared-store-daemon.md:26"),
        operation="add",
        rejected=[
            {
                "alternative": "Keep CLI-Only Embedded Access",
                "reason": _extracted_keep_cli_reason(),
            }
        ],
        confidence="high",
    )
    # The card came back filed (not rejected) and at num >= 2 (002, since the
    # scaffold seed holds 001).
    assert result["status"] == "confirmed", result
    assert result["decision_id"].startswith("002-"), result["decision_id"]
    return store_path


def test_seeded_card_yields_non_empty_check_decision(seeded_store: Path):
    """A num>=2 seeded decision surfaces on check_decision retrieval."""
    envelope = tool_check_decision(
        seeded_store, "Add a daemon to own shared store access for agents"
    )

    # The scaffold seed (001) is filtered from check_decision, so a non-empty
    # result proves the num>=2 card — not the seed — was retrieved.
    assert envelope["store"] == "local"
    assert envelope.get("error") is None
    assert envelope["related_decisions"], envelope
    assert envelope["assessment"], envelope

    titles = [hit["title"] for hit in envelope["related_decisions"]]
    assert any("daemon" in t.lower() for t in titles), titles
    # The deterministic assessment names a top match (the proactive hook's payload).
    assert "Top match" in envelope["assessment"], envelope["assessment"]


def test_citation_line_survives_into_seeded_decision(seeded_store: Path):
    """The Source: citation and the extractor's verbatim rejection both persist
    into the written decision — no paraphrase, no placeholder."""
    decisions = sorted((seeded_store / "decisions").glob("*.md"))
    card = next(d for d in decisions if d.name.startswith("002-"))
    body = card.read_text(encoding="utf-8")
    assert "Source: docs/adr/0003-shared-store-daemon.md:26" in body
    # The rationale span is a verbatim quote from the fixture, not composed for the test.
    assert _RATIONALE_SPAN in FIXTURE_ADR.read_text(encoding="utf-8")
    # The named rejected alternative carries the extractor's VERBATIM reason end
    # to end — not a paraphrase and not a placeholder.
    assert "Keep CLI-Only Embedded Access" in body
    assert _extracted_keep_cli_reason() in body
    assert "Rejected reason not available in source ADR." not in body


def test_proactive_check_decision_hook_present_on_floor():
    """The 1.0.1 instruction floor carries the proactive check_decision hook.

    Step 0's per-card pre-pass leans on the same proactive instruction that
    tells an agent to call check_decision before responding to a change request.
    Pin that the hook text ships, so the end-to-end retrieval above is wired to
    a real instruction and not just an in-test convenience.
    """
    assert "check_decision" in MCP_INSTRUCTIONS_STATIC
    assert "Before responding to any technical change request" in MCP_INSTRUCTIONS_STATIC
