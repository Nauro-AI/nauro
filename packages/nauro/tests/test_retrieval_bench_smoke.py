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

The Stage-0 disqualifier assertions stay structure-only too: the
artifact regime key is present, the paraphrase generator's round-trip invariant
holds (no verbatim rejected-name token survives), g's output is schema-valid,
and g abstains on the off-domain negative probe. NO precision/catch/score is
asserted in CI — semantic precision is operator-attested on a private store.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from nauro.demo import DEMO_DECISIONS, create_demo_project

SCRIPT = Path(__file__).resolve().parents[3] / "benchmarks" / "retrieval_bench.py"


def _load_bench_module():
    """Import the out-of-package benchmark script as a module for direct calls.

    The round-trip and schema assertions exercise the generator and g directly,
    not just the JSON the subprocess emits, so the structural invariants are
    pinned at the function boundary.
    """
    spec = importlib.util.spec_from_file_location("retrieval_bench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    # Stage-0: the artifact regime is present in the result schema.
    assert "artifact_regime" in result, "Stage-0 artifact regime missing from result"
    artifact = result["artifact_regime"]
    for key in ("n_fired", "surfacing_precision", "coverage", "exposure", "per_class"):
        assert key in artifact, f"artifact_regime missing {key!r}"
    assert "feasibility" in result and "verdict" in result["feasibility"]
    # g abstains on the off-domain negative probe (Tier-A negative probe).
    assert result["abstain_slice"]["correct_abstain"], result["abstain_slice"]["fires"]

    # Event model: both supersession directions, independently re-derived.
    expected_forward, expected_reverse = _expected_event_counts()
    assert expected_reverse > 0, "demo store lost its reverse-only consolidation fan"
    assert fingerprint["events_forward"] == expected_forward
    assert fingerprint["events_reverse_only"] == expected_reverse


def test_artifact_generator_roundtrip_invariant() -> None:
    """A generated positive contains no verbatim rejected-name token of its source.

    The round-trip property the precision number depends on: if the generator
    planted the rejected-name token g keys on, the precision would be an artifact
    of the generator. Checked on the demo store directly at the function boundary.
    """
    bench = _load_bench_module()
    decisions = {d.num: d for d in DEMO_DECISIONS}
    events, _ = bench.extract_events(decisions)
    artifacts = bench.generate_artifact_queries(decisions, events)
    assert artifacts, "no artifact queries generated on the demo store"
    for art in artifacts:
        superseder = decisions[art["q"]]
        forbidden = bench.rejected_name_tokens(superseder)
        query_tokens = {tok.strip(bench._STRIP_CHARS).lower() for tok in art["query"].split()}
        leaked = forbidden & query_tokens
        assert not leaked, f"event {art['q']}->{art['t']} leaked rejected-name tokens {leaked}"


def test_fire_predicate_schema_valid() -> None:
    """g's output is schema-valid whether it fires or abstains (structure-only)."""
    bench = _load_bench_module()
    decisions = {d.num: d for d in DEMO_DECISIONS}
    events, _ = bench.extract_events(decisions)
    artifacts = bench.generate_artifact_queries(decisions, events)

    sample = artifacts[0]
    candidates = bench.active_at(decisions, sample["q"])
    by_num = {d.num: d for d in candidates}
    active_numbers = set(by_num)
    ranked = bench.union_retrieve(
        candidates,
        sample["query"],
        top_k=len(candidates),
        stopwords=bench._CHECK_DECISION_STOPWORDS,
        use_embeddings=False,
    )
    top_hit = ranked[0] if ranked else None
    hit_decision = by_num.get(top_hit["number"]) if top_hit else None

    for hit, hit_d in ((top_hit, hit_decision), (None, None)):
        verdict = bench.fire_predicate(hit, sample["query"], active_numbers, hit_d)
        assert set(("fired", "outcome", "active", "carries")).issubset(verdict)
        assert isinstance(verdict["fired"], bool)
        # Abstain is "nothing matched", never "all clear".
        assert verdict["outcome"] in ("fired", bench.ABSTAIN)
        assert verdict["fired"] is (verdict["outcome"] == "fired")


def test_g_abstains_on_negative_probe() -> None:
    """g does not fire on the off-domain negatives on the demo store.

    The same Tier-A negative probe the subprocess asserts, pinned at the
    function boundary so a structural regression in g surfaces here directly.
    """
    bench = _load_bench_module()
    decisions = {d.num: d for d in DEMO_DECISIONS}
    slice_result = bench.run_abstain_slice(decisions)
    assert slice_result["correct_abstain"], slice_result["fires"]
