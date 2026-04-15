"""ExtractionProvider Protocol — interface for LLM-based extraction backends.

Defines a clean seam for multi-provider support (D51). Currently only
AnthropicProvider exists; OpenAI and Ollama can be added post-launch.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nauro.extraction.types import ExtractionOutcome


@runtime_checkable
class ExtractionProvider(Protocol):
    """Interface for LLM-based extraction backends."""

    def extract_from_diff(
        self,
        commit_message: str,
        diff_summary: str,
        changed_files: list[str],
        existing_decisions: list[str] | None = None,
    ) -> ExtractionOutcome: ...
