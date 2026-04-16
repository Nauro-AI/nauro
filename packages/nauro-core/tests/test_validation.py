"""Tests for nauro_core.validation — structural screening and BM25 similarity."""

from nauro_core.validation import (
    check_bm25_similarity,
    compute_hash,
    screen_structural,
)


class TestComputeHash:
    def test_deterministic(self):
        h1 = compute_hash("Use FastAPI", "Because async is great")
        h2 = compute_hash("Use FastAPI", "Because async is great")
        assert h1 == h2

    def test_case_insensitive(self):
        h1 = compute_hash("Use FastAPI", "Because async")
        h2 = compute_hash("use fastapi", "because async")
        assert h1 == h2

    def test_whitespace_normalized(self):
        h1 = compute_hash("  Use FastAPI  ", "  Because async  ")
        h2 = compute_hash("Use FastAPI", "Because async")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = compute_hash("Use FastAPI", "Because async")
        h2 = compute_hash("Use Flask", "Because simple")
        assert h1 != h2

    def test_returns_hex_string(self):
        h = compute_hash("Title", "Rationale")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex


class TestCheckBm25Similarity:
    def _decision(self, num, title, rationale="Some rationale text here.", status="active"):
        return {"num": num, "title": title, "rationale": rationale, "status": status}

    def test_no_existing_decisions(self):
        proposal = {"title": "Use FastAPI", "rationale": "Async support is great."}
        action, related = check_bm25_similarity(proposal, [])
        assert action == "auto_confirm"
        assert related == []

    def test_only_scaffold_seed_is_excluded(self):
        # A store containing only the scaffold-seed must never gate user proposals.
        existing = [self._decision(1, "Initial project setup", "Store was initialized.")]
        proposal = {
            "title": "Use Redis for caching",
            "rationale": "Fast in-memory store with pub/sub.",
        }
        action, related = check_bm25_similarity(proposal, existing)
        assert action == "auto_confirm"
        assert related == []

    def test_unrelated_proposal_auto_confirms(self):
        existing = [
            self._decision(2, "Chose FastAPI for the server", "Async support and automatic docs."),
        ]
        proposal = {
            "title": "Add dark mode to the UI",
            "rationale": "Users requested a dark theme for reduced eye strain.",
        }
        action, related = check_bm25_similarity(proposal, existing)
        assert action == "auto_confirm"
        assert related == []

    def test_vocabulary_mismatch_flagged(self):
        # D93 motivating case: BM25 + stemming catches vocabulary mismatches
        # that substring and naive word-set matching miss.
        existing = [
            self._decision(
                2,
                "Chose Memcached for session state",
                "Memcached is simpler than Redis for session caching. "
                "Lower operational overhead for our read-heavy workload.",
            )
        ]
        proposal = {
            "title": "Use Redis for session caching",
            "rationale": "Redis provides session state management with persistence.",
        }
        action, related = check_bm25_similarity(proposal, existing)
        assert action == "needs_review"
        assert any("Memcached" in r["title"] for r in related)

    def test_generic_use_verb_does_not_escalate(self):
        # Shared stopword-extended ``use`` must not produce a match on its own.
        existing = [self._decision(2, "Use FastAPI", "Good async support.")]
        proposal = {
            "title": "Use Redis for caching",
            "rationale": "Fast in-memory store for session data management.",
        }
        action, related = check_bm25_similarity(proposal, existing)
        assert action == "auto_confirm"
        assert related == []

    def test_result_shape_matches_bm25_retrieve(self):
        existing = [
            self._decision(
                2,
                "Use FastAPI for the server",
                "Async support and automatic OpenAPI documentation.",
            )
        ]
        proposal = {
            "title": "Use FastAPI for the API layer",
            "rationale": "FastAPI provides async and OpenAPI docs.",
        }
        action, related = check_bm25_similarity(proposal, existing)
        assert action == "needs_review"
        hit = related[0]
        assert "number" in hit
        assert "title" in hit
        assert "similarity" in hit
        assert "rationale_preview" in hit

    def test_respects_top_k(self):
        existing = [
            self._decision(
                n,
                "Use FastAPI for service",
                "Async support and automatic OpenAPI documentation.",
            )
            for n in range(2, 12)
        ]
        proposal = {
            "title": "Use FastAPI for the API layer",
            "rationale": "FastAPI provides async and OpenAPI docs.",
        }
        _, related = check_bm25_similarity(proposal, existing, top_k=3)
        assert len(related) <= 3

    def test_superseded_decisions_excluded(self):
        # bm25_retrieve ignores non-active decisions — shared function inherits
        # that filter. A superseded near-match must not trigger needs_review.
        existing = [
            self._decision(
                2,
                "Use FastAPI for the server",
                "Async support and automatic OpenAPI documentation.",
                status="superseded",
            )
        ]
        proposal = {
            "title": "Use FastAPI for the API layer",
            "rationale": "FastAPI provides async and OpenAPI docs.",
        }
        action, related = check_bm25_similarity(proposal, existing)
        assert action == "auto_confirm"
        assert related == []


class TestScreenStructural:
    def _proposal(self, **overrides):
        base = {
            "title": "Use FastAPI for MCP server",
            "rationale": "FastAPI provides async support and type safety for the server.",
            "confidence": "high",
        }
        base.update(overrides)
        return base

    def test_clean_pass(self):
        action, reason = screen_structural(self._proposal(), set(), [])
        assert action == "pass"
        assert reason is None

    def test_empty_title(self):
        action, reason = screen_structural(self._proposal(title=""), set(), [])
        assert action == "reject"
        assert "Title is empty" in reason

    def test_empty_rationale(self):
        action, reason = screen_structural(self._proposal(rationale=""), set(), [])
        assert action == "reject"
        assert "Rationale is empty" in reason

    def test_short_rationale(self):
        action, reason = screen_structural(self._proposal(rationale="Too short"), set(), [])
        assert action == "reject"
        assert "too short" in reason.lower()

    def test_invalid_confidence(self):
        action, reason = screen_structural(self._proposal(confidence="super"), set(), [])
        assert action == "reject"
        assert "Invalid confidence" in reason

    def test_hash_dedup(self):
        proposal = self._proposal()
        h = compute_hash(proposal["title"], proposal["rationale"])
        action, reason = screen_structural(proposal, {h}, [])
        assert action == "reject"
        assert "hash match" in reason.lower()

    def test_title_dedup_recent(self):
        proposal = self._proposal()
        recent = [{"title": "Use FastAPI for MCP server", "num": 42}]
        action, reason = screen_structural(proposal, set(), recent)
        assert action == "reject"
        assert "same title" in reason.lower()

    def test_title_dedup_case_insensitive(self):
        proposal = self._proposal()
        recent = [{"title": "use fastapi for mcp server", "num": 42}]
        action, reason = screen_structural(proposal, set(), recent)
        assert action == "reject"

    def test_default_confidence_accepted(self):
        proposal = self._proposal()
        del proposal["confidence"]
        action, reason = screen_structural(proposal, set(), [])
        assert action == "pass"

    def test_none_title(self):
        action, reason = screen_structural(self._proposal(title=None), set(), [])
        assert action == "reject"
        assert "Title is empty" in reason

    def test_none_rationale(self):
        action, reason = screen_structural(self._proposal(rationale=None), set(), [])
        assert action == "reject"
        assert "Rationale is empty" in reason
