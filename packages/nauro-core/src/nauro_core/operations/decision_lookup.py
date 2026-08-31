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
from typing import NamedTuple

from nauro_core.decision_model import Decision, parse_decision
from nauro_core.operations.store import Store
from nauro_core.parsing import (
    _decision_filename,
    _decision_number_prefix,
    extract_decision_number,
    sort_stems_by_number,
)

logger = logging.getLogger("nauro_core.operations.decision_lookup")


class ParseFailure(NamedTuple):
    """A decision file that did not round-trip through the v2 parser.

    ``stem`` is the on-disk file stem; ``error`` is the parser's message. The
    guarded scan captures these rather than dropping them so a caller that
    reports on store integrity (``doctor``) can name the offending file.
    """

    stem: str
    error: str


class ScannedDecision(NamedTuple):
    """A parsed decision paired with its on-disk carrier stem."""

    stem: str
    decision: Decision


def scan_decision_records(
    store: Store,
) -> tuple[list[ScannedDecision], list[ParseFailure], list[str]]:
    """Read every decision once, preserving parsed carrier stems and the full stem set.

    Results follow :func:`sort_stems_by_number`; parse failures never raise.
    """
    records: list[ScannedDecision] = []
    failures: list[ParseFailure] = []
    stems = sort_stems_by_number(store.list_decisions())
    bodies = store.read_decisions(stems)
    for stem in stems:
        body = bodies.get(stem)
        if body is None:
            continue
        try:
            decision = parse_decision(body, _decision_filename(stem))
            records.append(ScannedDecision(stem=stem, decision=decision))
        except Exception as exc:
            logger.debug("Skipping unparseable decision file: %s.md", stem)
            failures.append(ParseFailure(stem=stem, error=str(exc)))
    return records, failures, stems


def scan_decisions(store: Store) -> tuple[list[Decision], list[ParseFailure]]:
    """Read every decision, returning the parsed set and parse failures.

    The parsed list is ordered by :func:`sort_stems_by_number` and is unfiltered.
    """
    records, failures, _ = scan_decision_records(store)
    return [record.decision for record in records], failures


def parse_all_decisions(store: Store) -> list[Decision]:
    """Read and parse every decision in the store, discarding unparseable files.

    Thin wrapper over :func:`scan_decisions` that drops the parse failures.
    """
    parsed, _ = scan_decisions(store)
    return parsed


def parse_decision_or_none(body: str, filename: str) -> Decision | None:
    """Parse a single decision body, returning ``None`` if it does not parse.

    A file that fails to parse is logged at debug; the caller decides how to report.
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
