"""Tests for nauro_core.format — markdown protocol contract."""

from nauro_core.format import (
    METADATA_PATTERN,
    SECTION_PATTERN,
    TITLE_PATTERN,
    format_metadata_field,
    format_title,
    parse_metadata,
    parse_title,
)


class TestParseTitle:
    def test_standard_title(self):
        content = "# 001 \u2014 Use S3 for storage"
        num, title = parse_title(content)
        assert num == 1
        assert title == "Use S3 for storage"

    def test_high_number(self):
        content = "# 079 \u2014 Extract shared logic into nauro-core"
        num, title = parse_title(content)
        assert num == 79
        assert title == "Extract shared logic into nauro-core"

    def test_no_leading_zeros(self):
        content = "# 5 \u2014 Choose FastAPI"
        num, title = parse_title(content)
        assert num == 5
        assert title == "Choose FastAPI"

    def test_no_title_returns_none(self):
        content = "Some random content\nwith no title line"
        num, title = parse_title(content)
        assert num is None
        assert title is None

    def test_malformed_title_no_em_dash(self):
        content = "# 001 - Use S3 for storage"
        num, title = parse_title(content)
        assert num is None
        assert title is None

    def test_title_with_extra_whitespace(self):
        content = "# 010 \u2014   Trailing spaces  "
        num, title = parse_title(content)
        assert num == 10
        assert title == "Trailing spaces"

    def test_title_in_multiline_content(self):
        content = "Some preamble\n\n# 042 \u2014 Middle of file\n\nMore content"
        num, title = parse_title(content)
        assert num == 42
        assert title == "Middle of file"


class TestFormatTitle:
    def test_basic(self):
        assert format_title(1, "Use S3") == "# 001 \u2014 Use S3"

    def test_large_number(self):
        assert format_title(999, "Big decision") == "# 999 \u2014 Big decision"

    def test_round_trip(self):
        original_num, original_title = 42, "Round trip test"
        formatted = format_title(original_num, original_title)
        parsed_num, parsed_title = parse_title(formatted)
        assert parsed_num == original_num
        assert parsed_title == original_title


class TestParseMetadata:
    def test_single_field(self):
        content = "**Date:** 2026-04-01"
        result = parse_metadata(content)
        assert result == {"Date": "2026-04-01"}

    def test_multiple_fields(self):
        content = (
            "# 001 \u2014 Test\n"
            "**Date:** 2026-04-01\n"
            "**Status:** active\n"
            "**Confidence:** high\n"
            "**Type:** architecture\n"
        )
        result = parse_metadata(content)
        assert result["Date"] == "2026-04-01"
        assert result["Status"] == "active"
        assert result["Confidence"] == "high"
        assert result["Type"] == "architecture"

    def test_no_metadata(self):
        content = "Just plain text\nNo bold fields here"
        result = parse_metadata(content)
        assert result == {}

    def test_field_with_trailing_whitespace(self):
        content = "**Date:**   2026-04-01  "
        result = parse_metadata(content)
        assert result["Date"] == "2026-04-01"


class TestFormatMetadataField:
    def test_basic(self):
        assert format_metadata_field("Status", "active") == "**Status:** active"

    def test_round_trip_with_parse(self):
        formatted = format_metadata_field("Confidence", "high")
        result = parse_metadata(formatted)
        assert result["Confidence"] == "high"


class TestPatterns:
    def test_title_pattern_compiles(self):
        assert TITLE_PATTERN.pattern

    def test_metadata_pattern_compiles(self):
        assert METADATA_PATTERN.pattern

    def test_section_pattern_matches(self):
        m = SECTION_PATTERN.search("## Decision\nSome content")
        assert m
        assert m.group(1) == "Decision"

    def test_section_pattern_no_match(self):
        m = SECTION_PATTERN.search("# Not a section\nMore text")
        assert m is None
