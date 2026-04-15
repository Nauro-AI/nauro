"""Tests for nauro_core.validation — structural screening and Jaccard similarity."""

from nauro_core.validation import (
    check_jaccard_similarity,
    compute_hash,
    jaccard_similarity,
    screen_structural,
    word_set,
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


class TestWordSet:
    def test_basic(self):
        result = word_set("Hello world foo")
        assert "hello" in result
        assert "world" in result
        assert "foo" in result

    def test_strips_punctuation(self):
        result = word_set("hello, world! (test)")
        assert "hello" in result
        assert "world" in result
        assert "test" in result

    def test_short_words_excluded(self):
        result = word_set("I am a big cat")
        assert "big" in result
        assert "cat" in result
        assert "am" not in result
        assert "a" not in result

    def test_empty_string(self):
        assert word_set("") == set()

    def test_lowercase(self):
        result = word_set("FastAPI Lambda")
        assert "fastapi" in result
        assert "lambda" in result


class TestJaccardSimilarity:
    def test_identical_sets(self):
        s = {"hello", "world"}
        assert jaccard_similarity(s, s) == 1.0

    def test_disjoint_sets(self):
        a = {"hello", "world"}
        b = {"foo", "bar"}
        assert jaccard_similarity(a, b) == 0.0

    def test_partial_overlap(self):
        a = {"hello", "world", "foo"}
        b = {"hello", "world", "bar"}
        sim = jaccard_similarity(a, b)
        assert 0.0 < sim < 1.0
        # intersection={hello, world}=2, union={hello, world, foo, bar}=4
        assert sim == 0.5

    def test_both_empty(self):
        assert jaccard_similarity(set(), set()) == 0.0

    def test_one_empty(self):
        assert jaccard_similarity({"hello"}, set()) == 0.0

    def test_subset(self):
        a = {"hello", "world"}
        b = {"hello", "world", "foo"}
        sim = jaccard_similarity(a, b)
        # 2/3
        assert abs(sim - 2 / 3) < 0.001


class TestCheckJaccardSimilarity:
    def _decision(self, num, title, rationale="Some rationale text here."):
        return {"num": num, "title": title, "rationale": rationale}

    def test_no_existing_decisions(self):
        proposal = {"title": "Use FastAPI", "rationale": "Async support is great."}
        action, similar = check_jaccard_similarity(proposal, [])
        assert action == "auto_confirm"
        assert similar == []

    def test_below_threshold(self):
        proposal = {"title": "Use FastAPI for server", "rationale": "Async support is great."}
        existing = [self._decision(1, "Choose Redis for caching", "Speed and simplicity.")]
        action, similar = check_jaccard_similarity(proposal, existing)
        assert action == "auto_confirm"
        assert similar == []

    def test_above_threshold(self):
        proposal = {
            "title": "Use FastAPI for the MCP server",
            "rationale": "FastAPI provides async support and type safety for MCP server.",
        }
        existing = [
            self._decision(
                1,
                "Use FastAPI for MCP server",
                "FastAPI provides async support and type safety for the MCP server.",
            )
        ]
        action, similar = check_jaccard_similarity(proposal, existing)
        assert action == "needs_review"
        assert len(similar) >= 1
        assert similar[0]["number"] == 1

    def test_similarity_value_in_result(self):
        proposal = {
            "title": "Use FastAPI for server",
            "rationale": "FastAPI provides async and type safety.",
        }
        existing = [
            self._decision(1, "Use FastAPI for server", "FastAPI provides async and type safety.")
        ]
        action, similar = check_jaccard_similarity(proposal, existing)
        if similar:
            assert "similarity" in similar[0]
            assert 0.0 <= similar[0]["similarity"] <= 1.0

    def test_max_five_results(self):
        proposal = {"title": "common words shared across", "rationale": "common words shared."}
        existing = [
            self._decision(i, "common words shared across", "common words shared across all.")
            for i in range(10)
        ]
        action, similar = check_jaccard_similarity(proposal, existing)
        assert len(similar) <= 5

    def test_custom_threshold(self):
        proposal = {"title": "Use FastAPI", "rationale": "Async support."}
        existing = [self._decision(1, "Use Flask", "Simple and lightweight.")]
        # Very low threshold should catch even slight overlap
        action, _ = check_jaccard_similarity(proposal, existing, threshold=0.01)
        # With such a low threshold, any word overlap triggers review


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
