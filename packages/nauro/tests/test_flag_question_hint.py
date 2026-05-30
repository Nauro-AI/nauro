"""Tests for the flag_question similar-decision hint threshold.

The hint fires when the top BM25 match scores above
``FLAG_QUESTION_HINT_MIN_SCORE`` (a raw BM25 score, not a normalized 0-1
similarity). These tests pin the threshold behavior and the named constants
that replaced the previously-inlined literals.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from nauro.constants import (
    FLAG_QUESTION_HINT_MIN_SCORE,
    FLAG_QUESTION_HINT_TITLE_LENGTH,
)
from nauro.mcp.tools import tool_flag_question
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


def test_hint_constants_exist():
    assert FLAG_QUESTION_HINT_MIN_SCORE == 0.7
    assert FLAG_QUESTION_HINT_TITLE_LENGTH == 100


def _stub_similar(score: float) -> tuple[str, list[dict]]:
    return (
        "needs_review",
        [{"number": 7, "title": "Existing decision", "similarity": score}],
    )


def test_hint_fires_above_threshold(store):
    """A top score above the threshold annotates the flag with a hint."""
    above = FLAG_QUESTION_HINT_MIN_SCORE + 0.1
    with (
        patch("nauro.mcp.tools.check_bm25_similarity", return_value=_stub_similar(above)),
        patch("nauro.mcp.tools._try_push"),
    ):
        result = tool_flag_question(store, question="Should we cache hot reads?")

    assert "hint" in result
    assert "decision-007" in result["hint"]


def test_hint_absent_below_threshold(store):
    """A top score at or below the threshold leaves the flag without a hint."""
    below = FLAG_QUESTION_HINT_MIN_SCORE - 0.1
    with (
        patch("nauro.mcp.tools.check_bm25_similarity", return_value=_stub_similar(below)),
        patch("nauro.mcp.tools._try_push"),
    ):
        result = tool_flag_question(store, question="Should we cache hot reads?")

    assert "hint" not in result
