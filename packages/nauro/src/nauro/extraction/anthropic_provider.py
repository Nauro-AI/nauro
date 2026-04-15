"""Anthropic (Haiku) extraction provider.

Implements ExtractionProvider using the Anthropic SDK with tool_use
for structured output. This is the default provider.
"""

from __future__ import annotations

import logging
import os

try:
    import anthropic
except ImportError:
    raise ImportError("anthropic package required for extraction: pip install nauro[extraction]")

from nauro.constants import DEFAULT_EXTRACTION_MODEL, NAURO_EXTRACTION_MODEL_ENV
from nauro.extraction.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_TOOL,
    build_extraction_user_prompt,
)
from nauro.extraction.signal import SignalScore, compute_composite
from nauro.extraction.types import ExtractionOutcome, ExtractionResult, ExtractionSkipped

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Anthropic SDK-based extraction using Haiku with tool_use."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def _has_api_key(self) -> bool:
        return bool(self._api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def extract_from_diff(
        self,
        commit_message: str,
        diff_summary: str,
        changed_files: list[str],
        existing_decisions: list[str] | None = None,
    ) -> ExtractionOutcome:
        """Extract structured context from a commit diff using Anthropic Haiku."""
        if not self._has_api_key():
            return ExtractionSkipped(reason="no_api_key")

        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            model = os.environ.get(NAURO_EXTRACTION_MODEL_ENV, DEFAULT_EXTRACTION_MODEL)
            user_prompt = build_extraction_user_prompt(
                commit_message, diff_summary, changed_files, existing_decisions
            )

            response = client.messages.create(  # type: ignore[call-overload]
                model=model,
                max_tokens=1024,
                system=EXTRACTION_SYSTEM_PROMPT,
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "record_extraction"},
                messages=[{"role": "user", "content": user_prompt}],
            )

            for block in response.content:
                if block.type == "tool_use" and block.name == "record_extraction":
                    raw = block.input
                    signal_data = raw.get("signal", {})
                    signal = SignalScore(
                        architectural_significance=signal_data.get(
                            "architectural_significance", 0.0
                        ),
                        novelty=signal_data.get("novelty", 0.0),
                        rationale_density=signal_data.get("rationale_density", 0.0),
                        reversibility=signal_data.get("reversibility", 0.0),
                        scope=signal_data.get("scope", 0.0),
                        composite_score=raw.get("composite_score", 0.0),
                        reasoning=raw.get("reasoning", ""),
                    )

                    result = ExtractionResult(
                        decisions=raw.get("decisions", []),
                        questions=raw.get("questions", []),
                        state_delta=raw.get("state_delta"),
                        signal=signal,
                        skip=raw.get("skip", True),
                        reasoning=raw.get("reasoning", ""),
                    )

                    # Guard against false negatives: if the model returns
                    # skip=false with real decisions but composite_score=0.0,
                    # recompute the score server-side from signal dimensions.
                    if (
                        not result.skip
                        and result.decisions
                        and result.signal.composite_score == 0.0
                    ):
                        result.signal.composite_score = compute_composite(result.signal)

                    return result

            return ExtractionSkipped(reason="no_tool_use")

        except Exception:
            logger.debug("anthropic extraction failed", exc_info=True)
            return ExtractionSkipped(reason="error")
