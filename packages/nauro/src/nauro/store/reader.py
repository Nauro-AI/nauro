"""Store reader — read operations for the project store.

All reads from the .nauro/ project store go through this module.
"""

from pathlib import Path

from nauro_core import parse_decision
from nauro_core.decision_model import Decision
from nauro_core.parsing import sort_stems_by_number

from nauro.constants import DECISIONS_DIR


def read_text_lenient(path: Path) -> str:
    """Read a store file as UTF-8, replacing any undecodable byte.

    Freeform store markdown may carry legacy encodings; one bad byte must not abort a read.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _list_decisions(store_path: Path) -> list[Decision]:
    """Parse all decision files, return ``Decision`` objects sorted by number.

    The order is :func:`sort_stems_by_number`, not a sort on the filenames.
    """
    decisions_dir = store_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return []

    by_stem = {f.stem: f for f in decisions_dir.glob("*.md")}
    results: list[Decision] = []
    for stem in sort_stems_by_number(by_stem):
        path = by_stem[stem]
        results.append(parse_decision(read_text_lenient(path), path.name))
    return results
