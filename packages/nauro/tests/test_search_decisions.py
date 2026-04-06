"""Tests for search_decisions (D77 step 2)."""

from pathlib import Path

import pytest

from nauro.store.reader import search_decisions
from nauro.store.writer import append_decision
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Store with decisions for search testing."""
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)

    append_decision(
        store_path,
        "Use Auth0 for authentication",
        rationale="Auth0 provides OAuth 2.1 support and handles JWT validation. "
        "We need a managed identity provider to avoid unauthorized access.",
    )
    append_decision(
        store_path,
        "Redesign S3 key structure",
        rationale="CLI sync and remote MCP used different S3 key namespaces. "
        "Unifying to users/{sanitized_sub}/projects/{project_name}/ fixes cross-device sync.",
    )
    append_decision(
        store_path,
        "Use FastAPI for MCP server",
        rationale="FastAPI provides async support and automatic OpenAPI docs. "
        "Works well with Mangum for Lambda deployment.",
    )

    # Mark Auth0 decision as superseded
    for f in sorted((store_path / "decisions").glob("*.md")):
        if "auth0" in f.name:
            content = f.read_text()
            f.write_text(content.replace("**Status:** active", "**Status:** superseded"))
            break

    return store_path


def test_title_match(store: Path):
    result = search_decisions(store, "Auth0")
    assert result["store"] == "local"
    assert result["total_matches"] >= 1
    assert any("Auth0" in r["title"] for r in result["results"])


def test_rationale_match_with_snippet(store: Path):
    result = search_decisions(store, "Mangum")
    assert result["total_matches"] >= 1
    hit = next(r for r in result["results"] if "FastAPI" in r["title"])
    assert "Mangum" in hit["relevance_snippet"]


def test_case_insensitive_substring(store: Path):
    """'auth' matches 'Auth0', 'authentication', and 'unauthorized'."""
    result = search_decisions(store, "auth")
    titles = [r["title"] for r in result["results"]]
    assert any("Auth0" in t for t in titles)
    assert any("authentication" in t.lower() for t in titles)


def test_multi_word_any_match(store: Path):
    """'S3 key structure' matches decisions containing 'S3' even without 'key'."""
    result = search_decisions(store, "S3 key structure")
    assert any("S3" in r["title"] for r in result["results"])


def test_snippet_fallback_title_only(store: Path):
    """Title-only match uses first sentence of rationale as snippet."""
    result = search_decisions(store, "Redesign")
    hit = next(r for r in result["results"] if "Redesign" in r["title"])
    assert hit["relevance_snippet"]  # non-empty fallback


def test_empty_and_whitespace_query(store: Path):
    for q in ["", "   "]:
        result = search_decisions(store, q)
        assert "error" in result
        assert "non-empty" in result["error"]
        assert result["store"] == "local"


def test_limit(store: Path):
    result = search_decisions(store, "a", limit=1)
    assert len(result["results"]) <= 1
    assert result["total_matches"] >= len(result["results"])


def test_superseded_included_with_status(store: Path):
    result = search_decisions(store, "Auth0")
    statuses = {r["status"] for r in result["results"]}
    assert "superseded" in statuses


def test_sorted_descending(store: Path):
    result = search_decisions(store, "a")
    numbers = [r["number"] for r in result["results"]]
    assert len(numbers) >= 2
    assert numbers == sorted(numbers, reverse=True)
