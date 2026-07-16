"""Store validation — checks for common issues in the project store."""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from nauro_core import extract_decision_number

from nauro.constants import (
    CHARS_PER_TOKEN,
    DECISIONS_DIR,
    L0_TOKEN_LIMIT,
    PROJECT_MD,
    PROJECT_MD_TOKEN_WARN,
    STALE_SYNC_DAYS,
    STATE_CURRENT_FILENAME,
    STATE_LEGACY_FILENAME,
    VALIDATED_STORE_FILES,
)
from nauro.store.reader import read_text_lenient

logger = logging.getLogger("nauro.store.validator")


def validate_store(store_path: Path) -> list[str]:
    """Validate a project store and return a list of warnings.

    Checks:
    - Unfilled bracket prompts remaining in project.md, state.md, stack.md
    - state.md "Last synced" older than 7 days
    - L0 token count exceeds 3,500
    - project.md token count exceeds the L0-preamble warning threshold
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
        content = read_text_lenient(filepath)
        # Filter out markdown links [text](url) and timestamps [2026-01-01 ...]
        unfilled = []
        for m in re.finditer(r"\[([^\]]+)\]", content):
            p = m.group(1)
            if (
                len(p) >= 10
                and p[4] == "-"
                and p[7] == "-"
                and p[:4].isdigit()
                and p[5:7].isdigit()
                and p[8:10].isdigit()
            ):
                continue
            # Check if this bracket is part of a markdown link [text](...)
            end = m.end()
            if end < len(content) and content[end] == "(":
                continue
            unfilled.append(p)
        if unfilled:
            warnings.append(
                f"{filename}: {len(unfilled)} unfilled prompt(s) remaining - e.g. [{unfilled[0]}]"
            )

    # Check Last synced staleness — prefer state_current.md, fall back to legacy state.md.
    state_path = store_path / STATE_CURRENT_FILENAME
    if not state_path.exists():
        state_path = store_path / STATE_LEGACY_FILENAME
    if state_path.exists():
        content = read_text_lenient(state_path)
        synced_str: str | None = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("*Last synced:") and stripped.endswith("*"):
                synced_str = stripped[len("*Last synced:") : -1].strip()
                break
        if synced_str is not None:
            try:
                # Try parsing "YYYY-MM-DD HH:MM UTC" or "YYYY-MM-DD"
                if "UTC" in synced_str:
                    synced_date = datetime.strptime(synced_str, "%Y-%m-%d %H:%M UTC").replace(
                        tzinfo=timezone.utc
                    )
                else:
                    synced_date = datetime.strptime(synced_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )

                age = datetime.now(timezone.utc) - synced_date
                if age.days > STALE_SYNC_DAYS:
                    warnings.append(
                        f"{state_path.name}: Last synced {age.days} days ago - "
                        "consider running nauro sync"
                    )
            except ValueError:
                pass

    # Check actual L0 payload token estimate
    from nauro.mcp.payloads import build_l0_payload

    l0_payload = build_l0_payload(store_path)
    token_estimate = len(l0_payload) // CHARS_PER_TOKEN
    if token_estimate > L0_TOKEN_LIMIT:
        warnings.append(
            f"L0 token count ~{token_estimate} exceeds {L0_TOKEN_LIMIT:,} - consider trimming"
        )

    # Check project.md size — it leads every L0 payload as the scope preamble.
    project_path = store_path / PROJECT_MD
    if project_path.exists():
        project_estimate = len(read_text_lenient(project_path)) // CHARS_PER_TOKEN
        if project_estimate > PROJECT_MD_TOKEN_WARN:
            warnings.append(
                f"{PROJECT_MD}: ~{project_estimate} estimated tokens exceeds "
                f"{PROJECT_MD_TOKEN_WARN:,} - move detail to stack.md or decisions"
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
