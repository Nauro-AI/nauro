"""Shared helpers for skill-surface drift tests.

Centralises the surface registry and docs-prompt loader used by both
``test_skills_drift.py`` (phrase-level checks) and
``test_skill_tool_signatures.py`` (structural tool-call checks). Tests
iterate ``SKILL_SURFACES.items()`` directly when they want all surfaces,
or look up a single entry by key.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nauro_core import MCP_INSTRUCTIONS_STATIC

from nauro.skills import load_adopt_body, load_context_body, load_handoff_body

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_docs_adopt_prompt() -> str:
    return (REPO_ROOT / "docs" / "adopt-prompt.md").read_text(encoding="utf-8")


SKILL_SURFACES: dict[str, Callable[[], str]] = {
    "adopt_body.md": load_adopt_body,
    "handoff_body.md": load_handoff_body,
    "context_body.md": load_context_body,
    "MCP_INSTRUCTIONS_STATIC": lambda: MCP_INSTRUCTIONS_STATIC,
    "docs/adopt-prompt.md": load_docs_adopt_prompt,
}
