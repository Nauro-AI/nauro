"""State file splitting: prepare updates for state_current.md + state_history.md.

Pure functions that transform state content without any I/O. Callers are
responsible for reading/writing files (local filesystem or S3).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class StateUpdateResult:
    """Result of preparing a state update.

    Attributes:
        current_content: Full content to write to state_current.md.
        history_entry: Formatted block to append to state_history.md,
            or None if there was no prior state to archive.
    """

    current_content: str
    history_entry: str | None


def _utc_timestamp() -> str:
    """Return current UTC time as ISO 8601 with minute precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _strip_current_header_footer(content: str) -> str:
    """Strip the ``# Current State`` header and ``*Last updated: ...*`` footer."""
    lines = content.split("\n")
    stripped: list[str] = []
    for line in lines:
        if line.strip().startswith("# Current State"):
            continue
        if re.match(r"^\*Last updated:\s.*\*$", line.strip()):
            continue
        stripped.append(line)
    return "\n".join(stripped).strip()


def prepare_state_update(new_state: str, current_content: str | None) -> StateUpdateResult:
    """Prepare a state update for the split state files.

    Pure function, zero I/O. Takes the new state text and the current content
    of state_current.md (None if the file doesn't exist yet).

    Returns a StateUpdateResult with the new current content and an optional
    history entry to append to state_history.md.
    """
    timestamp = _utc_timestamp()

    new_current = f"# Current State\n\n{new_state}\n\n*Last updated: {timestamp}*\n"

    history_entry: str | None = None
    if current_content is not None:
        old_body = _strip_current_header_footer(current_content)
        if old_body:
            history_entry = f"## {timestamp}\n\n{old_body}\n\n---\n"

    return StateUpdateResult(current_content=new_current, history_entry=history_entry)


def migrate_legacy_state(legacy_content: str) -> StateUpdateResult:
    """Convert a pre-upgrade state.md into the split-state format.

    Returns a StateUpdateResult where current_content is the legacy content
    as-is (becomes state_current.md) and history_entry is None (no history
    to archive on first migration).
    """
    return StateUpdateResult(current_content=legacy_content, history_entry=None)


def assemble_state_for_context(
    current_content: str | None,
    history_content: str | None,
    include_history: bool = False,
) -> str | None:
    """Assemble state content for context payloads.

    Args:
        current_content: Content of state_current.md (or None).
        history_content: Content of state_history.md (or None).
        include_history: If False, returns current_content only (L0/L1).
            If True, concatenates both with a separator (L2).

    Returns:
        Assembled state string, or None if both inputs are None.
    """
    if not include_history:
        return current_content

    if current_content is not None and history_content is not None:
        return current_content + "\n\n# State History\n\n" + history_content
    if current_content is not None:
        return current_content
    if history_content is not None:
        return history_content
    return None
