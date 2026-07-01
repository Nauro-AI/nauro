"""Decision-stem lookup helpers shared across the operations kernel.

Resolving a decision identifier (any of the shapes
:func:`~nauro_core.parsing.extract_decision_number` accepts) to its
on-disk file stem only needs the :class:`~nauro_core.operations.store.Store`
protocol. Both ``propose_decision`` (supersede target resolution) and
``flag_question`` (resolve-action existence check) need it, so it lives
here rather than inside either operation module.
"""

from __future__ import annotations

import logging

from nauro_core.decision_model import Decision, parse_decision
from nauro_core.operations.store import Store
from nauro_core.parsing import (
    _decision_filename,
    _decision_number_prefix,
    extract_decision_number,
)

logger = logging.getLogger("nauro_core.operations.decision_lookup")


def parse_all_decisions(store: Store) -> list[Decision]:
    """Read and parse every decision in the store, skipping unparseable files.

    A single malformed file on disk (a half-written body, a pre-v2 file left
    during a migration) must not take down the read path. Each file that does
    not round-trip through the v2 parser is logged at debug and skipped, so
    the scan returns the decisions it could parse rather than raising.

    Bodies are fetched in one bulk :meth:`Store.read_decisions` call, but the
    scan still reasserts :meth:`Store.list_decisions` order: it iterates the
    stem list (not the returned mapping, which carries no ordering guarantee)
    so the parsed list follows ``list_decisions`` verbatim. That ordering is
    load-bearing — BM25 ranking breaks ties by corpus position, so a stable
    parse order keeps retrieval byte-identical. No filtering is applied here;
    callers that need a status or seed filter apply it after the scan returns.
    """
    parsed: list[Decision] = []
    stems = store.list_decisions()
    bodies = store.read_decisions(stems)
    for stem in stems:
        body = bodies.get(stem)
        if body is None:
            continue
        try:
            parsed.append(parse_decision(body, _decision_filename(stem)))
        except Exception:
            logger.debug("Skipping unparseable decision file: %s.md", stem)
            continue
    return parsed


def parse_decision_or_none(body: str, filename: str) -> Decision | None:
    """Parse a single decision body, returning ``None`` if it does not parse.

    The single-file analogue of :func:`parse_all_decisions`: a targeted
    lookup of a known file that fails to parse is logged at debug and
    surfaces as ``None`` so the caller can decide how to report it.
    """
    try:
        return parse_decision(body, filename)
    except Exception:
        logger.debug("Could not parse decision file: %s", filename)
        return None


def find_decision_stem_by_num(store: Store, num: int) -> str | None:
    """Return the file stem whose ``NNN-`` prefix matches ``num``, or None."""
    prefix = _decision_number_prefix(num)
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
