"""Store validation — checks for common issues in the project store."""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from nauro_core import extract_decision_number

from nauro.constants import (
    CHARS_PER_TOKEN,
    DECISIONS_DIR,
    L0_TOKEN_LIMIT,
    STALE_SYNC_DAYS,
    STATE_FIELD_LAST_SYNCED_ITALIC,
    STATE_MD,
    VALIDATED_STORE_FILES,
)

logger = logging.getLogger("nauro.store.validator")


def validate_store(store_path: Path) -> list[str]:
    """Validate a project store and return a list of warnings.

    Checks:
    - Unfilled bracket prompts remaining in project.md, state.md, stack.md
    - state.md "Last synced" older than 7 days
    - L0 token count exceeds 3,500
    - Decision numbering is sequential with no gaps

    Args:
        store_path: Path to the project store directory.

    Returns:
        List of warning strings (empty if no issues).
    """
    warnings: list[str] = []

    # Check for unfilled bracket prompts
    for filename in VALIDATED_STORE_FILES:
        filepath = store_path / filename
        if not filepath.exists():
            continue
        content = filepath.read_text()
        # Filter out markdown links [text](url) and timestamps [2026-01-01 ...]
        unfilled = []
        for m in re.finditer(r"\[([^\]]+)\]", content):
            p = m.group(1)
            if re.match(r"\d{4}-\d{2}-\d{2}", p):
                continue
            # Check if this bracket is part of a markdown link [text](...)
            end = m.end()
            if end < len(content) and content[end] == "(":
                continue
            unfilled.append(p)
        if unfilled:
            warnings.append(
                f"{filename}: {len(unfilled)} unfilled prompt(s) remaining — e.g. [{unfilled[0]}]"
            )

    # Check Last synced staleness
    state_path = store_path / STATE_MD
    if state_path.exists():
        content = state_path.read_text()
        sync_match = re.search(STATE_FIELD_LAST_SYNCED_ITALIC, content)
        if sync_match:
            synced_str = sync_match.group(1).strip()
            try:
                # Try parsing "YYYY-MM-DD HH:MM UTC" or "YYYY-MM-DD"
                if "UTC" in synced_str:
                    synced_date = datetime.strptime(synced_str, "%Y-%m-%d %H:%M UTC").replace(
                        tzinfo=UTC
                    )
                else:
                    synced_date = datetime.strptime(synced_str, "%Y-%m-%d").replace(tzinfo=UTC)

                age = datetime.now(UTC) - synced_date
                if age.days > STALE_SYNC_DAYS:
                    warnings.append(
                        f"state.md: Last synced {age.days} days ago — consider running nauro sync"
                    )
            except ValueError:
                pass

    # Check actual L0 payload token estimate
    from nauro.mcp.payloads import build_l0_payload

    l0_payload = build_l0_payload(store_path)
    token_estimate = len(l0_payload) // CHARS_PER_TOKEN
    if token_estimate > L0_TOKEN_LIMIT:
        warnings.append(
            f"L0 token count ~{token_estimate} exceeds {L0_TOKEN_LIMIT:,} — consider trimming"
        )

    # Check decision numbering is sequential with no gaps
    decisions_dir = store_path / DECISIONS_DIR
    if decisions_dir.exists():
        numbers = []
        for f in sorted(decisions_dir.glob("*.md")):
            n = extract_decision_number(f.name)
            if n is not None:
                numbers.append(n)
        if numbers:
            expected = list(range(numbers[0], numbers[-1] + 1))
            missing = set(expected) - set(numbers)
            if missing:
                warnings.append(f"Decision numbering gap: missing {sorted(missing)}")

    return warnings


def print_warnings(warnings: list[str]) -> None:
    """Print validation warnings to stderr.

    Args:
        warnings: List of warning strings from validate_store().
    """
    for w in warnings:
        logger.warning("  %s", w)
