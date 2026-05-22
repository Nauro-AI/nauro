"""Store reader — read operations for the project store.

All reads from the .nauro/ project store go through this module.
"""

from pathlib import Path

from nauro_core import extract_decision_number, parse_decision
from nauro_core.decision_model import Decision, DecisionStatus

from nauro.constants import DECISIONS_DIR


def _read_file(path: Path) -> str:
    """Read a file, return empty string if missing."""
    if path.exists():
        return path.read_text()
    return ""


def _list_decisions(store_path: Path) -> list[Decision]:
    """Parse all decision files, return ``Decision`` objects sorted by number."""
    decisions_dir = store_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return []

    results: list[Decision] = []
    for f in sorted(decisions_dir.glob("*.md")):
        content = f.read_text()
        results.append(parse_decision(content, f.name))
    return results


def resolve_decision_id(project_path: Path, identifier: str) -> str | None:
    """Resolve any decision id shape to the canonical file stem.

    Accepts whatever ``extract_decision_number`` accepts (file stem, synthetic
    ``decision-NNN``, ``DNNN``, or bare integer). Returns the on-disk file
    stem (e.g. ``"042-use-postgres"``); returns None if the identifier can't
    be parsed or no matching decision file exists.
    """
    num = extract_decision_number(identifier)
    if num is None:
        return None
    decisions_dir = project_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return None
    for f in decisions_dir.glob(f"{num:03d}-*.md"):
        return f.stem
    return None


def list_active_decisions(store_path: Path) -> list[Decision]:
    """Return only decisions with status=active."""
    return [d for d in _list_decisions(store_path) if d.status is DecisionStatus.active]


def get_decision_history(store_path: Path, decision_id: str) -> list[Decision]:
    """Follow the supersedes/superseded_by chain for a decision.

    Returns a list of decisions in chronological order (oldest first).
    """
    all_decisions = _list_decisions(store_path)
    decision_map = {str(d.num): d for d in all_decisions}

    target_num = extract_decision_number(decision_id)
    target = decision_map.get(str(target_num)) if target_num is not None else None
    if not target:
        return []

    chain: list[Decision] = [target]
    seen = {target.num}
    current = target
    while current.supersedes:
        prev = decision_map.get(current.supersedes)
        if not prev or prev.num in seen:
            break
        chain.insert(0, prev)
        seen.add(prev.num)
        current = prev

    current = target
    while current.superseded_by:
        nxt = decision_map.get(current.superseded_by)
        if not nxt or nxt.num in seen:
            break
        chain.append(nxt)
        seen.add(nxt.num)
        current = nxt

    return chain
