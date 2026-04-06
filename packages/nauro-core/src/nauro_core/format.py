"""Markdown protocol contract: compiled regexes, parse/format helpers.

Single source of truth for the Nauro markdown wire format. Both nauro CLI
and the remote MCP server delegate to these functions so that parsing and
formatting never diverge.
"""

import re

# ── Compiled patterns ──

# Matches: "# 001 — Title text" or "# 79 — Title text"
TITLE_PATTERN = re.compile(r"^#\s+(\d+)\s+\u2014\s+(.+)$", re.MULTILINE)

# Matches: "**Field:** value"
METADATA_PATTERN = re.compile(r"^\*\*(.+?):\*\*\s*(.+)$", re.MULTILINE)

# Matches: "## Section heading"
SECTION_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def parse_title(content: str) -> tuple[int | None, str | None]:
    """Extract (number, title) from a decision's H1 line.

    Returns (None, None) if no title line matches the protocol format.
    """
    m = TITLE_PATTERN.search(content)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None, None


def format_title(number: int, title: str) -> str:
    """Format a decision title line: ``# 001 — Title``."""
    return f"# {number:03d} \u2014 {title}"


def parse_metadata(content: str) -> dict[str, str]:
    """Extract all ``**Key:** value`` metadata fields from content.

    Returns a dict mapping field names to their string values.
    """
    return {m.group(1): m.group(2).strip() for m in METADATA_PATTERN.finditer(content)}


def format_metadata_field(field: str, value: str) -> str:
    """Format a single metadata field: ``**Field:** value``."""
    return f"**{field}:** {value}"
