"""Retrieval benchmark: conflict-catching against a store's own supersession history.

Measures the production retrieval kernel (the exact code behind search_decisions
and check_decision) on the one ground truth a Nauro store generates as a
byproduct of normal use: supersession events. For each decision Q that replaced
a decision T, the benchmark reconstructs the candidate set as it existed when Q
was filed and asks whether retrieval would have surfaced T in the top-K an
agent reads. A fixed generic novel-proposal battery provides the precision
counterweight, so a recall gain bought with a noisier score band is visible.

Event model: conflict events are the deduped union of forward ``supersedes``
edges and reverse ``superseded_by`` edges. The reverse direction is required
for correctness, not completeness: the ``supersedes`` frontmatter field is
single-valued, so one-to-many retirements exist only as reverse pointers. The
two classes report separately and are never blended; an N-member retirement
cannot fit a K-slot result list when N exceeds K, so a blended rate would be
uninterpretable by construction.

Scores and catch rates are corpus-relative. Outputs carry a corpus and
configuration fingerprint; ``--baseline`` warns on any fingerprint mismatch
before diffing metrics, so a code-change comparison is not silently conflated
with corpus growth.

Usage:
    uv run python benchmarks/retrieval_bench.py --store ~/.nauro/projects/<id>
    uv run python benchmarks/retrieval_bench.py --store PATH --embeddings
    uv run python benchmarks/retrieval_bench.py --store PATH --baseline prev.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib import metadata
from pathlib import Path

import bm25s
from nauro_core.decision_model import Decision, DecisionStatus, parse_decision
from nauro_core.operations.check_decision import _CHECK_DECISION_STOPWORDS
from nauro_core.parsing import first_sentence_end
from nauro_core.search import union_retrieve

BENCH_VERSION = "1"

# The fixed precision counterweight: generic engineering proposals with no
# project-private content, committed so contributors can run the precision
# side. Known rot mode: a query stops being novel the day the project decides
# that topic; review membership when the novel band shifts without a retrieval
# change. Amendments change the battery hash carried in every output, so old
# baselines are never silently compared against a different battery.
NOVEL_BATTERY = [
    "Add Kubernetes horizontal pod autoscaling for the web tier",
    "Switch the frontend framework to Svelte with server-side rendering",
    "Adopt GraphQL federation for the public API gateway",
    "Use Redis as a write-through cache for session tokens",
    "Migrate the primary datastore from Postgres to CockroachDB",
    "Add a Kafka event bus for asynchronous decision ingestion",
    "Introduce a Rust extension module for hot-path tokenization",
    "Build a React Native mobile client for browsing decisions",
    "Add WebAuthn passkey login as the primary auth method",
    "Compile the CLI to a single static Go binary",
    "Add real-time collaborative editing with CRDTs",
    "Train a fine-tuned LLM to auto-write decision rationales",
    "Replace the search index with an Elasticsearch cluster",
    "Add OpenTelemetry distributed tracing across request handlers",
    "Expose the API over gRPC instead of HTTP",
    "Store embeddings in a pgvector Postgres column",
    "Manage all cloud infrastructure with Terraform modules",
]

# Far outside any plausible decision store's domain. Production excludes
# zero-score documents, so these are expected to return nothing; a non-empty
# result here usually means the abstain cutoff regressed (though a store
# whose decisions genuinely share a stem with these can legitimately match;
# reported, not asserted). Chosen to share no stem with common engineering
# vocabulary: even words like "simulation", "assembly", or "firing" pull
# weak matches on a software store.
OFF_DOMAIN_BATTERY = [
    "quantum chromodynamics",
    "coral reef calcification",
    "glacial moraine sediment stratigraphy",
]

TOP_K = 5  # the related-decisions list length an agent reads (check_decision)
RANK_CUTOFFS = (1, 3, 5, 10)


def battery_hash() -> str:
    text = "\n".join(NOVEL_BATTERY + OFF_DOMAIN_BATTERY)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Store loading and event extraction
# ---------------------------------------------------------------------------


def load_decisions(store: Path) -> tuple[dict[int, Decision], int]:
    """Parse every decision file with the production parser.

    Returns (decisions by number, count of unparseable files). Unparseable
    files are skipped, matching the production read path's behavior.
    """
    decisions: dict[int, Decision] = {}
    unparseable = 0
    decisions_dir = store / "decisions"
    if not decisions_dir.is_dir():
        raise SystemExit(f"error: {decisions_dir} is not a directory")
    for path in sorted(decisions_dir.glob("*.md")):
        try:
            d = parse_decision(path.read_text(encoding="utf-8"), path.name)
        except Exception:
            unparseable += 1
            continue
        if d.num:
            decisions[d.num] = d
    if not decisions:
        raise SystemExit(f"error: no parseable decisions under {decisions_dir}")
    return decisions, unparseable


def _ref(value: str | None) -> int | None:
    return int(value) if value else None


def extract_events(decisions: dict[int, Decision]) -> tuple[list[dict], int]:
    """Deduped union of forward and reverse supersession edges.

    Forward: Q.supersedes names T. Reverse-only: T.superseded_by names Q but
    Q's single-valued supersedes field names some other decision (one-to-many
    retirements and retroactively repaired links live here). Events whose
    target does not precede the superseder are not reconstructable at filing
    time and are skipped with a count.
    """
    events: list[dict] = []
    seen: set[tuple[int, int]] = set()
    skipped_order = 0
    for n, d in sorted(decisions.items()):
        t = _ref(d.supersedes)
        if t is not None and t in decisions:
            seen.add((n, t))
            if t < n:
                events.append({"q": n, "t": t, "klass": "forward"})
            else:
                skipped_order += 1
    for n, d in sorted(decisions.items()):
        q = _ref(d.superseded_by)
        if q is None or q not in decisions or (q, n) in seen:
            continue
        seen.add((q, n))
        if n < q:
            events.append({"q": q, "t": n, "klass": "reverse_only"})
        else:
            skipped_order += 1
    return events, skipped_order


def active_at(decisions: dict[int, Decision], q: int) -> list[Decision]:
    """The candidate set as it existed when decision q was filed.

    Decisions numbered below q that were not yet superseded at that point.
    Statuses are rewritten to active because, at that moment, they were; the
    production retrieval path filters on status internally.
    """
    candidates = []
    for n in sorted(decisions):
        if n >= q:
            continue
        d = decisions[n]
        superseder = _ref(d.superseded_by)
        if superseder is None or superseder >= q:
            candidates.append(d.model_copy(update={"status": DecisionStatus.active}))
    return candidates


# ---------------------------------------------------------------------------
# Query construction (plain string ops per repo convention)
# ---------------------------------------------------------------------------

_STRIP_CHARS = "([)],.;:!?\"'"


def _drop_token(token: str) -> bool:
    """Drop tokens that leak the answer: explicit decision refs, wikilinks,
    and the supersession verb (an agent checking a proposal does not name the
    decision it is about to violate)."""
    if "[[" in token or "]]" in token:
        return True
    core = token.strip(_STRIP_CHARS)
    if core.startswith("D") and core[1:].isdigit() and 1 <= len(core) - 1 <= 4:
        return True
    return core.lower().startswith("supersede")


def clean_intent(text: str) -> str:
    return " ".join(token for token in text.split() if not _drop_token(token))


def first_sentence(d: Decision) -> str:
    rationale = d.rationale or ""
    if not rationale.strip():
        return ""
    return rationale[: first_sentence_end(rationale)]


def regime_query(d: Decision, regime: str) -> str:
    if regime == "title":
        return clean_intent(d.title)
    return clean_intent(f"{d.title}. {first_sentence(d)}")


def production_envelope(approach: str) -> str:
    """check_decision's exact query construction (first 100 chars doubled)."""
    return f"{approach[:100]}. {approach[:200]}"


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

REGIMES = ("title", "title_sent1")


def rank_of(target: int, hits: list[dict]) -> int | None:
    for i, hit in enumerate(hits):
        if hit["number"] == target:
            return i + 1
    return None


def run_conflict_catching(
    decisions: dict[int, Decision], events: list[dict], use_embeddings: bool
) -> tuple[dict, int]:
    """Catch@K / MRR per regime per class, plus the optional union arm."""
    results: dict = {
        regime: {
            klass: {
                "n": 0,
                **{f"catch@{k}": 0 for k in RANK_CUTOFFS},
                "mrr": 0.0,
                "union_pool_catch": 0,
                "union_appended": [],
            }
            for klass in ("forward", "reverse_only")
        }
        for regime in REGIMES
    }
    skipped_temporal = 0
    for event in events:
        q, t, klass = event["q"], event["t"], event["klass"]
        candidates = active_at(decisions, q)
        if not any(d.num == t for d in candidates):
            skipped_temporal += 1
            continue
        for regime in REGIMES:
            query = production_envelope(regime_query(decisions[q], regime))
            ranked = union_retrieve(
                candidates,
                query,
                top_k=len(candidates),
                stopwords=_CHECK_DECISION_STOPWORDS,
                use_embeddings=False,
            )
            rank = rank_of(t, ranked)
            bucket = results[regime][klass]
            bucket["n"] += 1
            for k in RANK_CUTOFFS:
                bucket[f"catch@{k}"] += rank is not None and rank <= k
            bucket["mrr"] += 1.0 / rank if rank else 0.0
            if use_embeddings:
                pool = union_retrieve(
                    candidates,
                    query,
                    top_k=TOP_K,
                    stopwords=_CHECK_DECISION_STOPWORDS,
                    use_embeddings=True,
                )
                bucket["union_pool_catch"] += t in [h["number"] for h in pool]
                bucket["union_appended"].append(max(0, len(pool) - TOP_K))
    for regime in REGIMES:
        for klass in ("forward", "reverse_only"):
            bucket = results[regime][klass]
            n = bucket["n"]
            bucket["mrr"] = round(bucket["mrr"] / n, 3) if n else None
            appended = sorted(bucket.pop("union_appended"))
            if use_embeddings and appended:
                bucket["union_appended_median"] = appended[len(appended) // 2]
            elif not use_embeddings:
                bucket.pop("union_pool_catch")
    return results, skipped_temporal


def run_batteries(decisions: dict[int, Decision]) -> dict:
    """Novel-band and off-domain abstain checks against the current active set."""
    active = [d for d in decisions.values() if d.status is DecisionStatus.active]
    novel_top1 = []
    for proposal in NOVEL_BATTERY:
        hits = union_retrieve(
            active,
            production_envelope(proposal),
            top_k=TOP_K,
            stopwords=_CHECK_DECISION_STOPWORDS,
            use_embeddings=False,
        )
        novel_top1.append(hits[0]["similarity"] if hits else 0.0)
    novel_top1.sort()
    off_domain = {}
    for query in OFF_DOMAIN_BATTERY:
        hits = union_retrieve(
            active,
            production_envelope(query),
            top_k=TOP_K,
            stopwords=_CHECK_DECISION_STOPWORDS,
            use_embeddings=False,
        )
        off_domain[query] = len(hits)
    return {
        "novel_top1_median": round(novel_top1[len(novel_top1) // 2], 2),
        "novel_top1_max": round(novel_top1[-1], 2),
        "off_domain_hits": off_domain,
    }


def build_fingerprint(
    decisions: dict[int, Decision],
    events: list[dict],
    skipped_order: int,
    skipped_temporal: int,
    unparseable: int,
    use_embeddings: bool,
) -> dict:
    statuses = [d.status for d in decisions.values()]
    try:
        core_version = metadata.version("nauro-core")
    except metadata.PackageNotFoundError:
        core_version = "unreleased"
    return {
        "bench_version": BENCH_VERSION,
        "nauro_core_version": core_version,
        "bm25s_version": getattr(bm25s, "__version__", "unknown"),
        "embeddings_arm": use_embeddings,
        "battery_hash": battery_hash(),
        "total_decisions": len(decisions),
        "active": sum(1 for s in statuses if s is DecisionStatus.active),
        "superseded": sum(1 for s in statuses if s is DecisionStatus.superseded),
        "highest_decision_number": max(decisions),
        "events_forward": sum(1 for e in events if e["klass"] == "forward"),
        "events_reverse_only": sum(1 for e in events if e["klass"] == "reverse_only"),
        "events_skipped_ordering": skipped_order,
        "events_skipped_temporal": skipped_temporal,
        "unparseable_files": unparseable,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(result: dict) -> None:
    fp = result["fingerprint"]
    print(
        f"retrieval_bench v{fp['bench_version']}  "
        f"nauro-core {fp['nauro_core_version']}  bm25s {fp['bm25s_version']}  "
        f"embeddings={'on' if fp['embeddings_arm'] else 'off'}"
    )
    print(
        f"store: {fp['total_decisions']} decisions "
        f"({fp['active']} active / {fp['superseded']} superseded, "
        f"highest D{fp['highest_decision_number']:03d}); "
        f"events: {fp['events_forward']} forward + {fp['events_reverse_only']} reverse-only "
        f"(skipped: {fp['events_skipped_ordering']} ordering, "
        f"{fp['events_skipped_temporal']} temporal)"
    )
    print()
    header = (
        f"{'regime':>12} {'class':>13} {'n':>4} | "
        + " ".join(f"{'c@' + str(k):>5}" for k in RANK_CUTOFFS)
        + f" | {'MRR':>6}"
    )
    if fp["embeddings_arm"]:
        header += f" | {'union':>6} {'app.':>5}"
    print(header)
    print("-" * len(header))
    for regime in REGIMES:
        for klass in ("forward", "reverse_only"):
            bucket = result["conflict_catching"][regime][klass]
            n = bucket["n"]
            if not n:
                continue
            row = (
                f"{regime:>12} {klass:>13} {n:>4} | "
                + " ".join(f"{bucket['catch@' + str(k)]:>5}" for k in RANK_CUTOFFS)
                + f" | {str(bucket['mrr']):>6}"
            )
            if fp["embeddings_arm"]:
                row += (
                    f" | {bucket.get('union_pool_catch', '-'):>6}"
                    f" {str(bucket.get('union_appended_median', '-')):>5}"
                )
            print(row)
    print()
    batteries = result["batteries"]
    print(
        f"novel battery: top-1 median {batteries['novel_top1_median']}, "
        f"max {batteries['novel_top1_max']}  (battery {fp['battery_hash']})"
    )
    abstain = ", ".join(f"{n}" for n in batteries["off_domain_hits"].values())
    print(f"off-domain hits (expected 0): {abstain}")
    print()
    print("Classes are reported separately by design: one-to-many retirements cannot")
    print("fit a top-K list, so reverse-only catch rates are slot-bounded. Catch rates")
    print("are corpus-relative; compare runs only through --baseline.")


def diff_baseline(result: dict, baseline: dict) -> None:
    print()
    print("=== baseline comparison ===")
    fp, bfp = result["fingerprint"], baseline.get("fingerprint", {})
    mismatches = [
        f"  {key}: baseline {bfp.get(key)!r} -> current {fp[key]!r}"
        for key in fp
        if bfp.get(key) != fp[key]
    ]
    if mismatches:
        print("WARNING: fingerprint mismatch; metric deltas below may reflect corpus,")
        print("battery, or dependency drift rather than the code change under test:")
        print("\n".join(mismatches))
    else:
        print("fingerprints match")
    for regime in REGIMES:
        for klass in ("forward", "reverse_only"):
            current = result["conflict_catching"][regime][klass]
            previous = baseline.get("conflict_catching", {}).get(regime, {}).get(klass, {})
            if not current["n"]:
                continue
            deltas = []
            for k in RANK_CUTOFFS:
                key = f"catch@{k}"
                if key in previous and previous[key] != current[key]:
                    deltas.append(f"{key} {previous[key]}->{current[key]}")
            if deltas:
                print(f"  {regime}/{klass}: " + ", ".join(deltas))
    print("=== end baseline comparison ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--store", required=True, type=Path, help="Store directory containing decisions/"
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        help="Also run the embeddings-union arm (requires the optional extra)",
    )
    parser.add_argument("--baseline", type=Path, help="Previous --json-out file to diff against")
    parser.add_argument("--json-out", type=Path, help="Write the full result JSON here")
    args = parser.parse_args(argv)

    if args.embeddings:
        from nauro_core.embeddings import embeddings_available

        if not embeddings_available():
            print(
                "error: --embeddings requires the optional extra "
                "(uv pip install 'nauro-core[embeddings]')",
                file=sys.stderr,
            )
            return 2

    decisions, unparseable = load_decisions(args.store.expanduser())
    events, skipped_order = extract_events(decisions)
    catching, skipped_temporal = run_conflict_catching(decisions, events, args.embeddings)
    result = {
        "fingerprint": build_fingerprint(
            decisions, events, skipped_order, skipped_temporal, unparseable, args.embeddings
        ),
        "conflict_catching": catching,
        "batteries": run_batteries(decisions),
    }
    print_report(result)
    if args.baseline:
        diff_baseline(result, json.loads(args.baseline.read_text(encoding="utf-8")))
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
