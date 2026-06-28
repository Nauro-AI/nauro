"""Store reader — read operations for the project store.

All reads from the .nauro/ project store go through this module.
"""

from pathlib import Path

from nauro_core import extract_decision_number, parse_decision
from nauro_core.decision_model import Decision, DecisionStatus

from nauro.constants import DECISIONS_DIR


def read_text_lenient(path: Path) -> str:
    """Read a store file as UTF-8, replacing any undecodable bytes.

    Store markdown is freeform and hand/agent-editable, so a file saved in a
    legacy encoding — a smart quote pasted from a non-UTF-8 editor, an imported
    doc in cp1252 — must not crash a read. Decoding with ``errors="replace"``
    keeps the whole read surface (get_context, sync, search) working on a
    mostly-valid file instead of aborting the command on a single bad byte.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _list_decisions(store_path: Path) -> list[Decision]:
    """Parse all decision files, return ``Decision`` objects sorted by number."""
    decisions_dir = store_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return []

    results: list[Decision] = []
    for f in sorted(decisions_dir.glob("*.md")):
        content = read_text_lenient(f)
        results.append(parse_decision(content, f.name))
    return results


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
