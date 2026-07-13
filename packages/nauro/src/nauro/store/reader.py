"""Store reader — read operations for the project store.

All reads from the .nauro/ project store go through this module.
"""

from pathlib import Path

from nauro_core import parse_decision
from nauro_core.decision_model import Decision

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
