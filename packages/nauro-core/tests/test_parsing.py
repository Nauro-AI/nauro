"""Tests for nauro_core.parsing — pure string→dict functions."""

from nauro_core.parsing import (
    decisions_summary_lines,
    extract_relevance_snippet,
    extract_stack_summary,
    parse_decision,
    parse_metadata_field,
    parse_questions,
    strip_frontmatter,
)


class TestStripFrontmatter:
    def test_with_frontmatter(self):
        content = "---\ntitle: test\ndate: 2026-01-01\n---\n# Body content"
        result = strip_frontmatter(content)
        assert result == "# Body content"

    def test_without_frontmatter(self):
        content = "# Just a heading\nSome content"
        result = strip_frontmatter(content)
        assert result == content

    def test_incomplete_frontmatter(self):
        content = "---\ntitle: test\nno closing marker"
        result = strip_frontmatter(content)
        assert result == content

    def test_empty_string(self):
        assert strip_frontmatter("") == ""


class TestParseMetadataField:
    def test_found(self):
        body = "**Date:** 2026-04-01\n**Status:** active"
        assert parse_metadata_field(body, "Date") == "2026-04-01"
        assert parse_metadata_field(body, "Status") == "active"

    def test_not_found(self):
        body = "No metadata here"
        assert parse_metadata_field(body, "Date") is None

    def test_field_with_special_chars(self):
        body = "**Files affected:** src/foo.py, src/bar.py"
        result = parse_metadata_field(body, "Files affected")
        assert result == "src/foo.py, src/bar.py"


class TestParseDecision:
    FULL_DECISION = (
        "# 042 \u2014 Use FastAPI for MCP server\n\n"
        "**Date:** 2026-04-01\n"
        "**Version:** 2\n"
        "**Status:** active\n"
        "**Confidence:** high\n"
        "**Type:** architecture\n"
        "**Reversibility:** moderate\n"
        "**Source:** mcp\n"
        "**Files affected:** src/server.py, src/routes.py\n"
        "**Supersedes:** 019-use-flask\n\n"
        "## Decision\n\n"
        "FastAPI was chosen for its async support and type safety.\n"
        "It integrates well with Mangum for Lambda deployment.\n\n"
        "## Rejected Alternatives\n\n"
        "### Flask\nNo native async support.\n"
    )

    def test_full_metadata(self):
        d = parse_decision(self.FULL_DECISION, "042-use-fastapi.md")
        assert d["num"] == 42
        assert d["title"] == "Use FastAPI for MCP server"
        assert d["date"] == "2026-04-01"
        assert d["version"] == 2
        assert d["status"] == "active"
        assert d["confidence"] == "high"
        assert d["decision_type"] == "architecture"
        assert d["reversibility"] == "moderate"
        assert d["source"] == "mcp"
        assert d["files_affected"] == ["src/server.py", "src/routes.py"]
        assert d["supersedes"] == "019-use-flask"
        assert d["superseded_by"] is None
        assert "FastAPI was chosen" in d["rationale"]
        assert d["body"]  # body should be non-empty
        assert d["content"] == self.FULL_DECISION

    def test_minimal_decision(self):
        content = "# 001 \u2014 Simple choice\n\n## Decision\n\nWe chose option A.\n"
        d = parse_decision(content, "001-simple-choice.md")
        assert d["num"] == 1
        assert d["title"] == "Simple choice"
        assert d["status"] == "active"  # default
        assert d["version"] == 1  # default
        assert d["confidence"] is None
        assert d["files_affected"] is None
        assert d["superseded_by"] is None
        assert d["supersedes"] is None

    def test_missing_status_defaults_active(self):
        content = "# 005 \u2014 No status field\n\n**Date:** 2026-01-01\n\n## Decision\n\nDone.\n"
        d = parse_decision(content, "005-no-status.md")
        assert d["status"] == "active"

    def test_superseded_decision(self):
        content = (
            "# 019 \u2014 Use Flask\n\n"
            "**Status:** superseded\n"
            "**Superseded by:** 042-use-fastapi\n\n"
            "## Decision\n\nFlask was chosen initially.\n"
        )
        d = parse_decision(content, "019-use-flask.md")
        assert d["status"] == "superseded"
        assert d["superseded_by"] == "042-use-fastapi"

    def test_old_format_with_frontmatter(self):
        content = (
            "---\ntitle: Old format\n---\n"
            "# 003 \u2014 Old format decision\n\n"
            "## Rationale\n\nThis was the old format with YAML frontmatter.\n"
        )
        d = parse_decision(content, "003-old-format.md")
        assert d["num"] == 3
        assert d["title"] == "Old format decision"
        assert "old format with YAML frontmatter" in d["rationale"]

    def test_filename_number_extraction(self):
        content = "# 100 \u2014 Test\n"
        d = parse_decision(content, "100-test.md")
        assert d["num"] == 100

    def test_non_numeric_filename(self):
        content = "# Test\n"
        d = parse_decision(content, "random.md")
        assert d["num"] == 0


class TestExtractStackSummary:
    def test_normal_stack(self):
        content = (
            "# Stack\n"
            "## Language\n"
            "- **Python 3.11+** \u2014 main language\n"
            "  - Chose over Go for ecosystem\n"
            "## Infrastructure\n"
            "- **AWS Lambda** \u2014 serverless\n"
        )
        result = extract_stack_summary(content)
        assert "## Language" in result
        assert "- **Python 3.11+**" in result
        assert "## Infrastructure" in result
        assert "- **AWS Lambda**" in result
        assert "Chose over Go" not in result  # indented reasoning excluded

    def test_empty_stack(self):
        content = "# Stack\n<!-- Tech choices with rationale and rejected alternatives -->"
        assert extract_stack_summary(content) == ""

    def test_blank_content(self):
        assert extract_stack_summary("") == ""
        assert extract_stack_summary("   ") == ""


class TestParseQuestions:
    def test_checkbox_format(self):
        content = (
            "# Open Questions\n"
            "- [2026-01-01 UTC] How does auth work?\n"
            "- [2026-01-02 UTC] What about caching?\n"
        )
        questions = parse_questions(content)
        assert len(questions) == 2
        assert "How does auth work?" in questions[0]

    def test_h3_format(self):
        content = "# Open Questions\n### Should we use Redis?\n"
        questions = parse_questions(content)
        assert len(questions) == 1
        assert "Should we use Redis?" in questions[0]

    def test_plain_bullets(self):
        content = "- First question\n- Second question\n"
        questions = parse_questions(content)
        assert len(questions) == 2

    def test_skips_resolved_section(self):
        content = "# Open Questions\n- Active question\n## Resolved\n- Old resolved question\n"
        questions = parse_questions(content)
        assert len(questions) == 1
        assert "Active question" in questions[0]

    def test_empty_content(self):
        assert parse_questions("") == []

    def test_sub_bullets_excluded(self):
        content = "- Top level\n  - Sub bullet\n- Another top\n"
        questions = parse_questions(content)
        assert len(questions) == 2


class TestDecisionsSummaryLines:
    def test_basic_formatting(self):
        decisions = [
            {"num": 42, "title": "Use FastAPI", "date": "2026-04-01"},
            {"num": 41, "title": "Choose S3", "date": "2026-03-30"},
        ]
        lines = decisions_summary_lines(decisions)
        assert lines[0] == "- D42 \u2014 Use FastAPI (2026-04-01)"
        assert lines[1] == "- D41 \u2014 Choose S3 (2026-03-30)"

    def test_missing_date(self):
        decisions = [{"num": 1, "title": "No date decision"}]
        lines = decisions_summary_lines(decisions)
        assert lines[0] == "- D1 \u2014 No date decision"

    def test_limit(self):
        decisions = [{"num": i, "title": f"D{i}"} for i in range(20)]
        lines = decisions_summary_lines(decisions, limit=5)
        assert len(lines) == 5

    def test_empty_list(self):
        assert decisions_summary_lines([]) == []


class TestExtractRelevanceSnippet:
    def test_match_found(self):
        text = "This is a long text about authentication and authorization patterns."
        snippet = extract_relevance_snippet(text, ["authentication"])
        assert "authentication" in snippet

    def test_no_match(self):
        text = "Nothing relevant here."
        snippet = extract_relevance_snippet(text, ["missing"])
        assert snippet == ""

    def test_case_insensitive(self):
        text = "FastAPI is the chosen framework."
        snippet = extract_relevance_snippet(text, ["fastapi"])
        assert "FastAPI" in snippet

    def test_ellipsis_prefix(self):
        text = "A" * 200 + " target word here " + "B" * 200
        snippet = extract_relevance_snippet(text, ["target"])
        assert snippet.startswith("...")

    def test_ellipsis_suffix(self):
        text = "A" * 200 + " target word here " + "B" * 200
        snippet = extract_relevance_snippet(text, ["target"])
        assert snippet.endswith("...")

    def test_short_text_no_ellipsis(self):
        text = "short target text"
        snippet = extract_relevance_snippet(text, ["target"])
        assert not snippet.startswith("...")
        assert not snippet.endswith("...")

    def test_empty_query_words(self):
        text = "Some text"
        snippet = extract_relevance_snippet(text, [])
        assert snippet == ""

    def test_custom_length(self):
        text = "X" * 100 + " target " + "Y" * 100
        short = extract_relevance_snippet(text, ["target"], length=20)
        long = extract_relevance_snippet(text, ["target"], length=200)
        assert len(short) < len(long)
