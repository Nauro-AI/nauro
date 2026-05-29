"""Decision-stem lookup helpers shared across the operations kernel.

Resolving a decision identifier (any of the shapes
:func:`~nauro_core.parsing.extract_decision_number` accepts) to its
on-disk file stem only needs the :class:`~nauro_core.operations.store.Store`
protocol. Both ``propose_decision`` (supersede target resolution) and
``flag_question`` (resolve-action existence check) need it, so it lives
here rather than inside either operation module.
"""

from __future__ import annotations

from nauro_core.operations.store import Store
from nauro_core.parsing import extract_decision_number


def find_decision_stem_by_num(store: Store, num: int) -> str | None:
    """Return the file stem whose ``NNN-`` prefix matches ``num``, or None."""
    prefix = f"{num:03d}-"
    for stem in store.list_decisions():
        if stem.startswith(prefix):
            return stem
    return None


def find_decision_stem_by_id(store: Store, decision_id: str) -> str | None:
    """Resolve any decision-id shape (stem, ``decision-NNN``, ``DNNN``, int) to a stem."""
    num = extract_decision_number(decision_id)
    if num is None:
        return None
    return find_decision_stem_by_num(store, num)
