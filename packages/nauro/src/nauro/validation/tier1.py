"""Tier 1 validation — structural screening.

Fast, deterministic checks with no LLM or embedding calls. < 10ms.
Delegates pure validation logic to nauro_core; handles filesystem I/O locally.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nauro_core import extract_decision_number
from nauro_core.validation import compute_hash
from nauro_core.validation import screen_structural as _screen_structural_pure

from nauro.constants import DECISION_HASHES_FILE, DECISIONS_DIR


def screen_structural(proposal: dict, project_path: Path) -> tuple[str, str | None]:
    """Run structural screening on a proposal.

    Loads hashes and recent decisions from the filesystem, then delegates
    to nauro_core's pure validation function.

    Returns:
        (action, reason) where action is "pass" or "reject".
    """
    hash_index = _load_hash_index(project_path)
    existing_hashes = set(hash_index.keys())

    # Load recent decisions (last 24h) for title dedup
    recent_decisions = _load_recent_decisions(project_path)

    return _screen_structural_pure(proposal, existing_hashes, recent_decisions)


def _load_recent_decisions(project_path: Path) -> list[dict]:
    """Load decisions from the last 24 hours for title dedup."""
    decisions_dir = project_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return []

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent = []

    for f in sorted(decisions_dir.glob("*.md"), reverse=True):
        content = f.read_text()
        # Parse title
        title = ""
        for line in content.split("\n"):
            if line.startswith("# "):
                title = re.sub(r"^# \d+[:\s—]+\s*", "", line).strip()
                break

        # Parse date
        date_match = re.search(r"\*\*Date:\*\*\s*(\S+)", content)
        if date_match:
            try:
                decision_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").replace(
                    tzinfo=UTC
                )
                if decision_date >= cutoff:
                    # Parse num from filename
                    num = extract_decision_number(f.name) or 0
                    recent.append({"title": title, "num": num, "date": date_match.group(1)})
            except ValueError:
                pass

    return recent


def _load_hash_index(project_path: Path) -> dict:
    """Load the decision hash index."""
    path = project_path / DECISION_HASHES_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_hash_index(project_path: Path, index: dict) -> None:
    """Save the decision hash index."""
    path = project_path / DECISION_HASHES_FILE
    path.write_text(json.dumps(index, indent=2) + "\n")


def update_hash_index(title: str, rationale: str, decision_id: str, project_path: Path) -> None:
    """Add a decision's hash to the index after a successful write."""
    content_hash = compute_hash(title, rationale)
    index = _load_hash_index(project_path)
    index[content_hash] = {
        "decision_id": decision_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    _save_hash_index(project_path, index)
