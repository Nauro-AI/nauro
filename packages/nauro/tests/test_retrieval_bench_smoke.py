"""Structural smoke for benchmarks/retrieval_bench.py against the demo store.

Scope is deliberately structural: the script completes, derives the expected
event count from BOTH supersession directions, and emits schema-valid JSON.
No catch, rank, or score assertions: catch rates are corpus-relative and a
legitimate retrieval change may move them; this smoke exists so the benchmark
script cannot rot silently at kernel API boundaries, not to gate retrieval
quality.

The event-count assertion is the mechanical guard on the event model: the
demo store's consolidation fan exists only as reverse ``superseded_by``
pointers (the ``supersedes`` field is single-valued), so a regression to
forward-only extraction changes the derived count and fails here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from nauro.demo import DEMO_DECISIONS, create_demo_project

SCRIPT = Path(__file__).resolve().parents[3] / "benchmarks" / "retrieval_bench.py"


def _expected_event_counts() -> tuple[int, int]:
    """Derive forward / reverse-only counts from the demo decisions directly.

    Independent of the script's extraction code by design: this re-derives the
    same union from the frontmatter fields so the two implementations check
    each other.
    """
    by_num = {d.num: d for d in DEMO_DECISIONS}
    forward = set()
    for d in DEMO_DECISIONS:
        if d.supersedes and int(d.supersedes) in by_num and int(d.supersedes) < d.num:
            forward.add((d.num, int(d.supersedes)))
    reverse_only = set()
    for d in DEMO_DECISIONS:
        if not d.superseded_by:
            continue
        q = int(d.superseded_by)
        if q in by_num and (q, d.num) not in forward and d.num < q:
            reverse_only.add((q, d.num))
    return len(forward), len(reverse_only)


def test_bench_smoke_on_demo_store(tmp_path: Path) -> None:
    store = tmp_path / "demo-store"
    create_demo_project(store)
    json_out = tmp_path / "bench.json"

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--store", str(store), "--json-out", str(json_out)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    result = json.loads(json_out.read_text(encoding="utf-8"))

    # Schema: the keys every consumer (baseline diff, PR citation) relies on.
    fingerprint = result["fingerprint"]
    for key in (
        "bench_version",
        "battery_hash",
        "total_decisions",
        "events_forward",
        "events_reverse_only",
    ):
        assert key in fingerprint, f"fingerprint missing {key!r}"
    assert set(result["conflict_catching"]) == {"title", "title_sent1"}
    assert "novel_top1_median" in result["batteries"]

    # Event model: both supersession directions, independently re-derived.
    expected_forward, expected_reverse = _expected_event_counts()
    assert expected_reverse > 0, "demo store lost its reverse-only consolidation fan"
    assert fingerprint["events_forward"] == expected_forward
    assert fingerprint["events_reverse_only"] == expected_reverse
