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
from collections.abc import Iterator
from typing import NamedTuple

from nauro_core.decision_model import Decision, DecisionStatus, parse_decision
from nauro_core.operations.store import Store
from nauro_core.parsing import (
    _decision_filename,
    _decision_number_prefix,
    extract_decision_number,
    sort_stems_by_number,
)

logger = logging.getLogger("nauro_core.operations.decision_lookup")

# Parsed decisions kept across calls in a long-lived process, keyed by stem and
# holding the exact body text each one was parsed from. A hit needs the freshly
# read body to equal the stored one, so an edit, a restore or a same-size swap
# is a miss; a parse failure is never stored. The Decision objects handed out
# are shared: callers derive changes with ``model_copy`` and never mutate them.
_PARSED_BY_STEM: dict[str, tuple[str, Decision]] = {}


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
    stems = sort_stems_by_number(store.list_decisions())
    records, failures = _parse_stems(store, stems)
    _forget_unlisted(stems)
    return records, failures, stems


def _parse_stems(
    store: Store, stems: list[str]
) -> tuple[list[ScannedDecision], list[ParseFailure]]:
    """Read and parse ``stems`` in the given order, skipping missing and unparseable files."""
    records: list[ScannedDecision] = []
    failures: list[ParseFailure] = []
    bodies = store.read_decisions(stems)
    for stem in stems:
        body = bodies.get(stem)
        if body is None:
            continue
        try:
            decision = _parse_memoized(stem, body)
            records.append(ScannedDecision(stem=stem, decision=decision))
        except Exception as exc:
            logger.debug("Skipping unparseable decision file: %s.md", stem)
            failures.append(ParseFailure(stem=stem, error=str(exc)))
    return records, failures


def _parse_memoized(stem: str, body: str) -> Decision:
    """Return the parsed decision for ``body``, reusing the last parse of ``stem``
    when the body is unchanged. Raises whatever ``parse_decision`` raises.
    """
    cached = _PARSED_BY_STEM.get(stem)
    if cached is not None and cached[0] == body:
        return cached[1]
    decision = parse_decision(body, _decision_filename(stem))
    _PARSED_BY_STEM[stem] = (body, decision)
    return decision


def _forget_unlisted(stems: list[str]) -> None:
    """Drop memoized parses for stems the store no longer lists."""
    listed = set(stems)
    for stem in list(_PARSED_BY_STEM):
        if stem not in listed:
            _PARSED_BY_STEM.pop(stem, None)


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


# Tail-walk batch size: one bulk read per batch, and a batch grows past this
# size rather than split a run of stems that share one number.
_TAIL_WALK_BATCH = 32


def parse_recent_active_decisions(store: Store, count: int) -> list[Decision]:
    """Parse the newest active decisions, walking down from the highest number and
    stopping once ``count`` are in hand. Returns them in corpus order, so any
    projection of the full active scan that keeps only its tail is unchanged.
    """
    stems = sort_stems_by_number(store.list_decisions())
    if any(extract_decision_number(stem) is None for stem in stems):
        return [d for d in parse_all_decisions(store) if d.status is DecisionStatus.active]
    _forget_unlisted(stems)
    active: list[Decision] = []
    if count <= 0:
        return active
    for batch in _tail_batches(stems):
        records, _ = _parse_stems(store, batch)
        active[:0] = [r.decision for r in records if r.decision.status is DecisionStatus.active]
        if len(active) >= count:
            break
    return active


def _tail_batches(stems: list[str]) -> Iterator[list[str]]:
    """Yield ``stems`` from the end in ascending-order batches of roughly
    ``_TAIL_WALK_BATCH``, never splitting stems that share one decision number.
    """
    end = len(stems)
    while end > 0:
        start = max(end - _TAIL_WALK_BATCH, 0)
        boundary_num = extract_decision_number(stems[start])
        while start > 0 and extract_decision_number(stems[start - 1]) == boundary_num:
            start -= 1
        yield stems[start:end]
        end = start


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
