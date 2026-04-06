"""Typed extraction results for the pipeline.

Replaces raw dicts with dataclasses so consumers get attribute access
and isinstance checks instead of magic-string .get() patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nauro.extraction.signal import SignalScore


@dataclass
class ExtractionResult:
    """Successful extraction from a commit."""

    decisions: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    state_delta: str | None = None
    signal: SignalScore = field(default_factory=SignalScore)
    skip: bool = False
    reasoning: str = ""

    def to_dict(self) -> dict:
        """Convert to dict for route_extraction_to_store and logging."""
        return {
            "decisions": self.decisions,
            "questions": self.questions,
            "state_delta": self.state_delta,
            "signal": self.signal.to_dict(),
            "composite_score": self.signal.composite_score,
            "skip": self.skip,
            "reasoning": self.reasoning,
        }


@dataclass
class ExtractionSkipped:
    """Extraction was skipped (no API key, error, no tool_use block)."""

    reason: str  # "no_api_key" | "error" | "no_tool_use"


ExtractionOutcome = ExtractionResult | ExtractionSkipped
