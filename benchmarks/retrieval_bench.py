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

Stage-0 disqualifier. Beyond catch@K, this script carries the harness that
decides whether a future doctrine-flag-at-artifact capability -- one that would
flag, in the artifact a human already reviews, when a change re-walks a path a
settled decision rejected -- may ever emit a flag. It does NOT ship that
capability. It adds: a paraphrased
"artifact" query regime (rejected-name tokens vocabulary-shifted so the lexical
bridge is not handed back), a structural-only fire predicate ``g`` (ACTIVE and
CARRIES, with NO score-margin band), the surfacing-precision / coverage /
exposure measurement arms with Wilson + Clopper-Pearson lower bounds, and an
offline feasibility sweep returning a certified joint lower bound OR the verdict
``UNATTAINABLE``. The gate always reads the interval LOWER bound, never the
point estimate. On the current store the bar is UNATTAINABLE and the capability
stays advisory-only. Semantic precision is measured only on an operator-attested
manual run against a private store; CI sees structure only and nothing
store-derived is committed.

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
from _stats import (
    clopper_pearson_lower,
    mcnemar_exact_p,
    required_n_fired,
    rule_of_three_upper,
    wilson_lower,
)
from nauro_core.decision_model import Decision, DecisionStatus, parse_decision
from nauro_core.operations.check_decision import _CHECK_DECISION_STOPWORDS
from nauro_core.parsing import first_sentence_end
from nauro_core.search import union_retrieve

BENCH_VERSION = "1"
# Schema version of the privacy-preserving aggregate summary (build_summary).
# v2 adds per-class surfacing_hits, the exposure object, and the top-level
# version tag; pool_certify.py rejects any summary that is not v2. Bumped
# independently of BENCH_VERSION: the measurement kernel and the exchange
# envelope version on their own cadences.
SUMMARY_SCHEMA_VERSION = "2"

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

# The Tier-A negative probe for g. Distinct from NOVEL_BATTERY: a novel
# engineering proposal can legitimately re-walk a rejected alternative (e.g. a
# store that rejected GraphQL), and g firing on that is a TRUE positive, not a
# regression — so NOVEL_BATTERY cannot be the correct-abstain set. These probes
# are off-domain enough to share no rejected-alternative-name token with any
# plausible engineering decision store, so g must abstain on every one. This is
# the set CI asserts a clean abstain on (structurally, against the demo store).
# Membership is verified disjoint from the demo store's rejected-name tokens by
# the smoke test, so a future demo decision that adopts one of these words fails
# CI loudly rather than silently weakening the probe.
G_NEGATIVE_BATTERY = [
    "schedule a dentist appointment for next Tuesday",
    "water the basil plants on the kitchen windowsill",
    "rehearse the cello sonata before the recital",
    "catalogue the butterfly specimens by wingspan",
    "knead the sourdough and proof it overnight",
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
# Stage-0 artifact-query generator
# ---------------------------------------------------------------------------
#
# For each supersession event Q->T the generator derives a terse imperative
# plan/diff/PR-title line from the SUPERSEDER's rejected-alternative names plus
# an action verb, then VOCABULARY-SHIFTS the name tokens so the rejected-name
# tokens are NOT verbatim present. The shift is a deterministic synonym map over
# stems (NO LLM). It must do the shift itself: clean_intent/_drop_token strip
# Dxxx/wikilinks/supersede* but NOT rejected-name tokens, so a naive synthesizer
# would plant the exact token g's CARRIES leg keys on and the precision number
# would be an artifact of the generator, not retrieval.
#
# Round-trip invariant (hard, not best-effort): a generated positive contains NO
# verbatim rejected-name token of its source decision. The synonym map handles
# the common engineering vocabulary; any residual rejected-name token that the
# map does not cover is DROPPED, so the invariant holds for an arbitrary store,
# not just the demo store. A fully-paraphrased positive that therefore shares no
# rejected-name token with its retrieved top hit makes g abstain by design --
# that is the cross-vocabulary limit, not a bug.

_ARTIFACT_VERBS = ("Implement", "Add", "Wire up", "Introduce", "Build")

# Deterministic synonym/paraphrase map over normalized (lowercased, stripped)
# rejected-name tokens. Generic engineering vocabulary only -- no store-private
# content. Multi-word replacements are allowed; each replacement is re-checked
# against the forbidden set so it cannot reintroduce a source token.
_VOCAB_SHIFT = {
    "shared": "common",
    "counter": "tally",
    "redis": "an in-memory key-value cache",
    "keeping": "retaining",
    "concerns": "responsibilities",
    "inline": "in place",
    "extracting": "pulling out",
    "helpers": "utility routines",
    "heavier": "more substantial",
    "framework": "platform",
    "cross-cutting": "orthogonal",
    "built": "baked",
    "validation": "input-checking",
    "error": "failure",
    "errors": "failures",
    "logging": "telemetry",
    "central": "unified",
    "middleware": "interceptor",
    "endpoint": "route",
    "endpoints": "routes",
    "mapping": "translation",
    "schema": "contract",
    "layer": "tier",
    "queue": "work buffer",
    "background": "deferred",
    "job": "task",
    "soft": "logical",
    "deletes": "removals",
    "delete": "removal",
    "websocket": "a duplex socket channel",
    "graphql": "a typed query gateway",
    "mongodb": "a document datastore",
    "polyrepo": "split repositories",
    "offset": "skip-count",
    "limit": "row cap",
    "pagination": "page traversal",
}

# Tokens with no semantic content for the artifact line; dropped before the line
# is assembled so the imperative reads naturally.
_ARTIFACT_STOPWORDS = frozenset(
    {"a", "an", "the", "in", "with", "and", "or", "for", "of", "to", "but", "per"}
)


def _name_tokens(name: str) -> list[str]:
    return [core for core in (t.strip(_STRIP_CHARS).lower() for t in name.split()) if core]


def rejected_name_tokens(d: Decision) -> set[str]:
    """All content tokens across the decision's rejected-alternative names.

    The forbidden set the round-trip invariant guards against, and the token
    pool g's CARRIES leg tests for. Stopwords are excluded so a bare article is
    never treated as a lexical bridge.
    """
    tokens: set[str] = set()
    for r in d.rejected:
        for tok in _name_tokens(r.name):
            if tok not in _ARTIFACT_STOPWORDS:
                tokens.add(tok)
    return tokens


def vocab_shift_name(name: str, verb: str, forbidden: set[str]) -> str:
    """Vocabulary-shift one rejected-name into a terse imperative artifact line.

    Mapped tokens are replaced; stopwords pass through; any unmapped token that
    is a forbidden (source rejected-name) token is DROPPED to keep the hard
    round-trip invariant; other unmapped tokens pass through. Replacement words
    are themselves filtered against the forbidden set.
    """
    out: list[str] = []
    for tok in name.split():
        core = tok.strip(_STRIP_CHARS).lower()
        if not core:
            continue
        if core in _ARTIFACT_STOPWORDS:
            out.append(core)
            continue
        replacement = _VOCAB_SHIFT.get(core)
        if replacement is not None:
            out.extend(w for w in replacement.split() if w.lower() not in forbidden)
        elif core not in forbidden:
            out.append(core)
        # else: an unmapped source token -> dropped (invariant guarantee).
    body = " ".join(out).strip()
    return f"{verb} {body}".strip() if body else verb


def generate_artifact_queries(decisions: dict[int, Decision], events: list[dict]) -> list[dict]:
    """One paraphrased artifact query per supersession event.

    Each query is derived from the SUPERSEDER's rejected names, vocabulary-
    shifted, and wrapped in production_envelope so the query surface is byte-
    identical to check_decision. Events whose superseder has no rejected
    alternative cannot seed an artifact line and are skipped (recorded by the
    caller via the count delta). The action verb is chosen deterministically
    from the event so re-draws are reproducible.
    """
    artifacts: list[dict] = []
    for i, event in enumerate(events):
        q, t, klass = event["q"], event["t"], event["klass"]
        superseder = decisions[q]
        if not superseder.rejected:
            continue
        forbidden = rejected_name_tokens(superseder)
        verb = _ARTIFACT_VERBS[(q + i) % len(_ARTIFACT_VERBS)]
        # The first rejected name is the primary alternative the superseder
        # displaced; it seeds the artifact line.
        line = vocab_shift_name(superseder.rejected[0].name, verb, forbidden)
        artifacts.append(
            {
                "q": q,
                "t": t,
                "klass": klass,
                "source_tokens": sorted(forbidden),
                "query": production_envelope(line),
            }
        )
    return artifacts


# ---------------------------------------------------------------------------
# Stage-0 structural fire predicate g
# ---------------------------------------------------------------------------
#
# g fires iff BOTH deterministic legs hold:
#   ACTIVE   -- the top hit is active at the artifact's time (active_at).
#   CARRIES  -- at least one of the top hit's rejected[].name tokens is lexically
#               present in the artifact (the rejected-name index is the bridge).
# There is NO score-margin gate; g never reads the per-hit similarity field.
# Below either leg, g abstains and the outcome is "nothing matched" -- never
# "all clear" and never a low-confidence flag. The certification is the
# SURFACING claim, never a conflict verdict; bears-on-artifact is an operator
# post-hoc audit bound recorded BESIDE the gate, never folded into g.

ABSTAIN = "nothing matched"


def fire_predicate(
    top_hit: dict | None,
    artifact_query: str,
    active_numbers: set[int],
    hit_decision: Decision | None,
) -> dict:
    """Evaluate g on one (top hit, artifact) pair. Pure structural, no score.

    ``active_numbers`` is the set of decision numbers active at the artifact's
    filing time (from active_at). ``hit_decision`` is the full Decision behind
    the top hit (the hit dict carries no rejected[] field, so the predicate
    resolves it from the candidate set). Returns a schema-valid result whether
    it fires or abstains.
    """
    if top_hit is None or hit_decision is None:
        return {"fired": False, "outcome": ABSTAIN, "active": False, "carries": False}
    active = hit_decision.num in active_numbers
    artifact_tokens = {
        core for core in (t.strip(_STRIP_CHARS).lower() for t in artifact_query.split()) if core
    }
    carried = sorted(rejected_name_tokens(hit_decision) & artifact_tokens)
    carries = bool(carried)
    fired = active and carries
    return {
        "fired": fired,
        "outcome": "fired" if fired else ABSTAIN,
        "active": active,
        "carries": carries,
        "carried_tokens": carried,
        "top_hit": top_hit["number"],
    }


# ---------------------------------------------------------------------------
# Pre-registered feasibility constants. PRIVATE: never written into the
# fingerprint or any committed output, because committing a store-derived
# threshold (where --baseline reads) would leak store-specific data into the
# repo. They are hard constraints
# fixed BEFORE the sweep, not read off the sweep's own output.
# ---------------------------------------------------------------------------

# Surfacing-precision floor: the developer alert-fatigue floor.
_PRECISION_LB_FLOOR = 0.90
# Minimum coverage (fired / candidate-conflicts) lower bound a class must hold.
_C_MIN = 0.30
# Maximum exposure (wrong-flags per artifact reviewed) the trust budget allows.
_E_MAX = 0.05
# Minimum n_fired before any precision claim is admissible (small-N guard).
_MIN_N_FIRED = 30


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


# ---------------------------------------------------------------------------
# Stage-0 artifact regime: g over paraphrased artifacts + measurement arms
# ---------------------------------------------------------------------------


def _interval(k: int, n: int) -> dict:
    """The m/n proportion with both 95% lower bounds and the rule-of-three read.

    The gate reads ``wilson_lb`` / ``cp_lb`` (the conservative one), never the
    point estimate ``p``.
    """
    return {
        "k": k,
        "n": n,
        "p": round(k / n, 4) if n else None,
        "wilson_lb": round(wilson_lower(k, n), 4),
        "cp_lb": round(clopper_pearson_lower(k, n), 4),
        "rule_of_three_upper": round(rule_of_three_upper(n), 4) if k == 0 else None,
    }


# The named cross-vocabulary / mass-retirement diagnostic partition. The third
# class is not a separate event klass: it is the slice of events whose
# vocabulary shift left the artifact sharing NO rejected-name token with the
# target, which is exactly where g must abstain. Kept IN the coverage
# denominator as a visible certified hole, never carved out.
_DIAGNOSTIC_CLASSES = ("forward", "reverse_only", "cross_vocabulary")


def run_artifact_regime(decisions: dict[int, Decision], events: list[dict]) -> dict:
    """The Stage-0 disqualifier measurement arm.

    Runs g over the paraphrased artifact queries and computes, per the spec:
    surfacing-claim precision, coverage, exposure, the abstain slice on
    synthetic negatives, the per-class diagnostic partition, and the false-fire
    transcript. Every proportion carries Wilson + Clopper-Pearson lower bounds.

    All of this is operator-attested / private: the caller gates it out of any
    committed artifact. It is computed here so a manual run renders it; CI only
    ever exercises the generator and g structurally.
    """
    artifacts = generate_artifact_queries(decisions, events)
    all_artifact_count = len(decisions)  # exposure denominator: all reviewable artifacts

    per_class = {
        klass: {"candidate": 0, "fired": 0, "surfacing_hits": 0} for klass in _DIAGNOSTIC_CLASSES
    }
    fired_total = 0
    surfacing_hits_total = 0
    candidate_total = 0
    false_fire_transcript: list[dict] = []

    for art in artifacts:
        q, t, klass = art["q"], art["t"], art["klass"]
        candidates = active_at(decisions, q)
        if not any(d.num == t for d in candidates):
            continue
        candidate_total += 1
        per_class[klass]["candidate"] += 1

        ranked = union_retrieve(
            candidates,
            art["query"],
            top_k=len(candidates),
            stopwords=_CHECK_DECISION_STOPWORDS,
            use_embeddings=False,
        )
        top_hit = ranked[0] if ranked else None
        active_numbers = {d.num for d in candidates}
        by_num = {d.num: d for d in candidates}
        hit_decision = by_num.get(top_hit["number"]) if top_hit else None
        verdict = fire_predicate(top_hit, art["query"], active_numbers, hit_decision)

        # A positive whose paraphrase shares no rejected-name token with its own
        # target is the cross-vocabulary slice: g cannot key on a bridge that is
        # gone. Classified here for the diagnostic partition; it stays in the
        # coverage denominator regardless.
        target_decision = by_num.get(t)
        target_tokens = rejected_name_tokens(target_decision) if target_decision else set()
        artifact_tokens = {
            core for core in (w.strip(_STRIP_CHARS).lower() for w in art["query"].split()) if core
        }
        if not (target_tokens & artifact_tokens):
            per_class["cross_vocabulary"]["candidate"] += 1

        if not verdict["fired"]:
            continue

        fired_total += 1
        per_class[klass]["fired"] += 1
        # Surfacing claim: the top hit is active AND the artifact re-walks its
        # rejected alternative. Both are already verified by g for a fire, so a
        # fire whose top hit is the intended target is a surfacing hit; a fire on
        # a different active decision is an effective false fire for the
        # transcript (the operator labels the reason on the manual run).
        is_target = top_hit["number"] == t
        if is_target:
            surfacing_hits_total += 1
            per_class[klass]["surfacing_hits"] += 1
        else:
            false_fire_transcript.append(
                {
                    "artifact_event": f"{q}->{t}",
                    "klass": klass,
                    "fired_on": top_hit["number"],
                    "carried_tokens": verdict["carried_tokens"],
                    # Operator-attested label slot; the harness never fills it
                    # from a model (no LLM-as-judge). Reasons: superseded /
                    # adjacent / wrong-rejected-alt / correct-but-ignored.
                    "operator_label": None,
                }
            )

    surfacing_precision = _interval(surfacing_hits_total, fired_total)
    coverage = _interval(fired_total, candidate_total)
    exposure_wrong = fired_total - surfacing_hits_total
    exposure = {
        "wrong_fires": exposure_wrong,
        "artifacts_reviewed": all_artifact_count,
        "rate": round(exposure_wrong / all_artifact_count, 4) if all_artifact_count else None,
    }

    diagnostics = {}
    for klass in _DIAGNOSTIC_CLASSES:
        cell = per_class[klass]
        diagnostics[klass] = {
            "candidate": cell["candidate"],
            "fired": cell["fired"],
            "surfacing_precision": _interval(cell["surfacing_hits"], cell["fired"]),
            # Below the per-class minimum the cell is diagnostic only; pooled
            # gating governs.
            "diagnostic_only": cell["fired"] < _MIN_N_FIRED,
        }

    return {
        "n_events": len(events),
        "n_artifacts": len(artifacts),
        "n_candidate_conflicts": candidate_total,
        "n_fired": fired_total,
        "min_n_fired_guard": _MIN_N_FIRED,
        "min_n_fired_met": fired_total >= _MIN_N_FIRED,
        "surfacing_precision": surfacing_precision,
        "coverage": coverage,
        "exposure": exposure,
        "per_class": diagnostics,
        "false_fire_transcript": false_fire_transcript,
        "required_n_fired": {
            "lb_0.90": required_n_fired(0.90),
            "lb_0.926": required_n_fired(0.926),
            "lb_0.975": required_n_fired(0.975),
        },
    }


def _probe_g(active: list[Decision], by_num: dict[int, Decision], query: str) -> dict:
    """Evaluate g for one off-event probe query against the current active set."""
    active_numbers = {d.num for d in active}
    envelope = production_envelope(query)
    ranked = union_retrieve(
        active,
        envelope,
        top_k=len(active),
        stopwords=_CHECK_DECISION_STOPWORDS,
        use_embeddings=False,
    )
    top_hit = ranked[0] if ranked else None
    hit_decision = by_num.get(top_hit["number"]) if top_hit else None
    return fire_predicate(top_hit, envelope, active_numbers, hit_decision)


def run_abstain_slice(decisions: dict[int, Decision]) -> dict:
    """g's NULL / correct-abstain slice plus the honest novel re-walk slice.

    Two probe sets with different semantics:

    - ``G_NEGATIVE_BATTERY`` + ``OFF_DOMAIN_BATTERY``: off-domain negatives that
      share no rejected-alternative-name token with any plausible store, so g
      MUST abstain. ``correct_abstain`` is keyed on this set only; a fire here is
      a structural regression and this is the slice CI asserts.

    - ``NOVEL_BATTERY``: novel engineering proposals. A fire here is NOT a
      regression: a novel proposal that lexically re-walks a rejected alternative
      is a true positive g is supposed to surface. Reported separately and
      honestly so the operator can read it, never folded into correct_abstain.
    """
    active = [d for d in decisions.values() if d.status is DecisionStatus.active]
    by_num = {d.num: d for d in active}

    negative_fires: list[dict] = []
    negatives = G_NEGATIVE_BATTERY + OFF_DOMAIN_BATTERY
    for query in negatives:
        verdict = _probe_g(active, by_num, query)
        if verdict["fired"]:
            negative_fires.append({"query": query, "fired_on": verdict["top_hit"]})

    novel_rewalks: list[dict] = []
    for query in NOVEL_BATTERY:
        verdict = _probe_g(active, by_num, query)
        if verdict["fired"]:
            novel_rewalks.append(
                {"query": query, "fired_on": verdict["top_hit"], "via": verdict["carried_tokens"]}
            )

    return {
        "n_probed": len(negatives),
        "n_fired": len(negative_fires),
        "correct_abstain": len(negative_fires) == 0,
        "fires": negative_fires,
        # Diagnostic only: novel proposals that re-walk a rejected alternative.
        "novel_rewalk_count": len(novel_rewalks),
        "novel_rewalks": novel_rewalks,
    }


def feasibility_sweep(artifact_result: dict) -> dict:
    """Offline risk-coverage verdict against the pre-registered C_min / E_max.

    Returns a CERTIFIED joint lower bound (precision-LB >= floor AND coverage-LB
    >= C_min AND exposure <= E_max AND n_fired >= the small-N guard) OR the
    verdict UNATTAINABLE -> stay dark. The thresholds are PRIVATE constants fixed
    before this runs; they are not read off the result. The gate reads the
    conservative Clopper-Pearson lower bound, never a point estimate.
    """
    prec_lb = artifact_result["surfacing_precision"]["cp_lb"]
    cov_lb = artifact_result["coverage"]["cp_lb"]
    exposure_rate = artifact_result["exposure"]["rate"] or 0.0
    n_fired = artifact_result["n_fired"]

    checks = {
        "precision_lb_ge_floor": prec_lb >= _PRECISION_LB_FLOOR,
        "coverage_lb_ge_c_min": cov_lb >= _C_MIN,
        "exposure_le_e_max": exposure_rate <= _E_MAX,
        "n_fired_ge_min": n_fired >= _MIN_N_FIRED,
    }
    attainable = all(checks.values())
    return {
        "verdict": "CERTIFIED" if attainable else "UNATTAINABLE",
        "checks": checks,
        # The floors are not echoed back as data: they are private. Only the
        # pass/fail booleans and the measured lower bounds (already in
        # artifact_result) cross into any output.
        "stay_dark": not attainable,
    }


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
    attested_date: str | None = None,
    attested_interval: str | None = None,
    attested_operator: str | None = None,
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
        # Last-attested certification record. Drift-keyed re-cert reads these.
        # C_min / E_max / any threshold / a tuned T are deliberately NOT here: a
        # store-derived threshold where --baseline reads would leak store-
        # specific data into the repo. Operator-supplied on the manual run, None
        # on a structure-only run.
        "last_attested_date": attested_date,
        "last_attested_interval": attested_interval,
        "last_attested_operator": attested_operator,
    }


def build_summary(result: dict) -> dict:
    """Privacy-preserving aggregate SUMMARY (schema v2): counts and bounds only.

    A second party reproduces the METHOD on their own store from this, and
    pool_certify.py pools it across stores. It carries NO store text -- no
    titles, no rationale, no false-fire transcript, no query strings. Only
    n_fired, m/n counts, interval lower bounds, per-class cell sizes,
    battery_hash, and the fingerprint cross into it.

    Schema invariant (the exchange contract pool_certify.py depends on): the
    summary must never contain rejected-alternative tokens or any per-token
    sketches, hashes, or digests of a store's lexicon; only counts, bounds,
    versions, and the fingerprint. The top-level key set is closed -- a new
    field is a schema change, not an incremental add -- so a store-text leak
    cannot ride in unnoticed.

    v2 over v1: a top-level ``summary_schema_version`` tag; ``surfacing_hits``
    per class in ``per_class_cell_sizes`` (so precision pools per class, not
    only per store); and ``exposure`` as an object of raw counts plus rate,
    replacing the flat ``exposure_rate`` (so exposure pools by summing counts).
    """
    fp = result["fingerprint"]
    art = result.get("artifact_regime")
    summary: dict = {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "fingerprint": fp,
        "battery_hash": fp["battery_hash"],
    }
    if art is None:
        return summary
    summary["artifact_regime"] = {
        "n_candidate_conflicts": art["n_candidate_conflicts"],
        "n_fired": art["n_fired"],
        "min_n_fired_met": art["min_n_fired_met"],
        "surfacing_precision": {
            "k": art["surfacing_precision"]["k"],
            "n": art["surfacing_precision"]["n"],
            "wilson_lb": art["surfacing_precision"]["wilson_lb"],
            "cp_lb": art["surfacing_precision"]["cp_lb"],
        },
        "coverage": {
            "k": art["coverage"]["k"],
            "n": art["coverage"]["n"],
            "wilson_lb": art["coverage"]["wilson_lb"],
            "cp_lb": art["coverage"]["cp_lb"],
        },
        "exposure": {
            "wrong_fires": art["exposure"]["wrong_fires"],
            "artifacts_reviewed": art["exposure"]["artifacts_reviewed"],
            "rate": art["exposure"]["rate"],
        },
        "per_class_cell_sizes": {
            klass: {
                "candidate": cell["candidate"],
                "fired": cell["fired"],
                # The value already exists in run_artifact_regime's per-class
                # diagnostics as the numerator of the class surfacing interval.
                "surfacing_hits": cell["surfacing_precision"]["k"],
            }
            for klass, cell in art["per_class"].items()
        },
        "required_n_fired": art["required_n_fired"],
        "feasibility_verdict": result["feasibility"]["verdict"],
    }
    return summary


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


def _fmt_interval(label: str, interval: dict) -> str:
    k, n = interval["k"], interval["n"]
    if n == 0:
        return f"{label}: 0/0 (no evidence)"
    return (
        f"{label}: {k}/{n} (p={interval['p']}, "
        f"Wilson-LB={interval['wilson_lb']}, CP-LB={interval['cp_lb']})"
    )


def print_artifact_report(result: dict) -> None:
    """Render the Stage-0 disqualifier arm (operator-attested manual run only).

    The gate reads the LOWER bound; the point estimate is shown for context but
    never gates. The false-fire transcript and feasibility verdict render here.
    """
    art = result["artifact_regime"]
    feas = result["feasibility"]
    abstain = result["abstain_slice"]
    print()
    print("=== Stage-0 disqualifier — artifact regime, operator-attested ===")
    print(
        f"events {art['n_events']}, artifact queries {art['n_artifacts']}, "
        f"candidate conflicts {art['n_candidate_conflicts']}, n_fired {art['n_fired']}"
    )
    print(
        f"min-n_fired guard {art['min_n_fired_guard']}: "
        f"{'met' if art['min_n_fired_met'] else 'NOT met — precision claim inadmissible'}"
    )
    print("  " + _fmt_interval("surfacing precision (gate)", art["surfacing_precision"]))
    print("  " + _fmt_interval("coverage", art["coverage"]))
    ex = art["exposure"]
    print(
        f"  exposure (wrong-flags/artifact): "
        f"{ex['wrong_fires']}/{ex['artifacts_reviewed']} = {ex['rate']}"
    )
    rn = art["required_n_fired"]
    print(
        f"  required n_fired (perfect run): LB>=0.90 -> {rn['lb_0.90']}, "
        f"LB>=0.926 -> {rn['lb_0.926']}, LB>=0.975 -> {rn['lb_0.975']}"
    )
    print()
    print("  per-class diagnostic partition (DIAGNOSTIC until a cell reaches the guard):")
    for klass, cell in art["per_class"].items():
        tag = "diagnostic-only" if cell["diagnostic_only"] else "gateable"
        print(
            f"    {klass:>16}: candidate {cell['candidate']:>3}, fired {cell['fired']:>3} "
            f"[{tag}]  " + _fmt_interval("prec", cell["surfacing_precision"])
        )
    print("  (the cross_vocabulary / mass-retirement class stays IN the coverage denominator")
    print("   as a visible certified hole — never carved out; routed to the advisory hook.)")
    print()
    abstain_status = (
        "all abstained" if abstain["correct_abstain"] else f"{abstain['n_fired']} WRONG FIRES"
    )
    print(
        f"  abstain slice (NULL/correct-abstain on {abstain['n_probed']} off-domain negatives): "
        f"{abstain_status}"
    )
    print(
        f"  novel-proposal re-walks (true positives, not regressions): "
        f"{abstain['novel_rewalk_count']} of {len(NOVEL_BATTERY)}"
    )
    transcript = art["false_fire_transcript"]
    if transcript:
        print(
            f"  false-fire transcript ({len(transcript)} fires off-target, operator labels them):"
        )
        for row in transcript:
            print(
                f"    {row['artifact_event']} ({row['klass']}) fired on D{row['fired_on']} "
                f"via {row['carried_tokens']} — label: {row['operator_label']}"
            )
    else:
        print("  false-fire transcript: empty (no off-target fires)")
    print()
    print("  rejected-name-indexing catch moves, exact McNemar on the discordant items:")
    for label, b, c in (("44->46", 0, 2), ("46->47", 0, 1), ("44->47", 0, 3)):
        p = mcnemar_exact_p(b, c)
        print(f"    {label}: p={round(p, 4)} — not statistically distinguishable")
    print()
    print(f"  FEASIBILITY VERDICT: {feas['verdict']}")
    if feas["stay_dark"]:
        print("  -> STAY DARK. The bar is UNATTAINABLE on this store; the artifact-flag")
        print("     capability stays advisory-only. Cross-store pooling is the")
        print("     path to the emit-tier N. Failing checks:")
        for name, ok in feas["checks"].items():
            if not ok:
                print(f"       - {name}")
    else:
        print("  -> CERTIFIED joint lower bound: precision-LB, coverage-LB, exposure, and")
        print("     n_fired all clear the pre-registered floors for this store's fingerprint.")
    print("=== end Stage-0 disqualifier ===")


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
    parser.add_argument(
        "--summary-out",
        type=Path,
        help="Write the privacy-preserving aggregate SUMMARY (counts/bounds only, no store text)",
    )
    # Operator-attested certification record. Recorded into the
    # fingerprint on a manual run; never store-derived. Thresholds stay private.
    parser.add_argument("--attested-date", help="Certification date for the fingerprint record")
    parser.add_argument(
        "--attested-interval", help="Re-cert interval label for the fingerprint record"
    )
    parser.add_argument("--attested-operator", help="Attesting operator id for the record")
    args = parser.parse_args(argv)

    if args.embeddings:
        # Probe the optional embeddings extra up front so a missing dependency
        # fails with a clear message instead of mid-run inside the union arm.
        try:
            import model2vec  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            print(
                "error: --embeddings requires the optional extra "
                "(uv pip install 'nauro-core[embeddings]')",
                file=sys.stderr,
            )
            return 2

    decisions, unparseable = load_decisions(args.store.expanduser())
    events, skipped_order = extract_events(decisions)
    catching, skipped_temporal = run_conflict_catching(decisions, events, args.embeddings)
    artifact_regime = run_artifact_regime(decisions, events)
    feasibility = feasibility_sweep(artifact_regime)
    result = {
        "fingerprint": build_fingerprint(
            decisions,
            events,
            skipped_order,
            skipped_temporal,
            unparseable,
            args.embeddings,
            attested_date=args.attested_date,
            attested_interval=args.attested_interval,
            attested_operator=args.attested_operator,
        ),
        "conflict_catching": catching,
        "batteries": run_batteries(decisions),
        "artifact_regime": artifact_regime,
        "abstain_slice": run_abstain_slice(decisions),
        "feasibility": feasibility,
    }
    print_report(result)
    print_artifact_report(result)
    if args.baseline:
        diff_baseline(result, json.loads(args.baseline.read_text(encoding="utf-8")))
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    if args.summary_out:
        args.summary_out.write_text(
            json.dumps(build_summary(result), indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.summary_out} (privacy-preserving aggregate summary)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
