"""Cross-store pooler / certifier for retrieval-benchmark surfacing summaries.

Out-of-package developer tooling, stdlib-only. It takes the privacy-preserving
schema-v2 summaries that ``retrieval_bench.py --summary-out`` emits (counts and
bounds only, never store text), pools them across independently operated stores,
and reports whether the pooled surfacing claim clears the pre-registered
feasibility bar. A single store is years from the high-stakes lower bound; the
only path to the emit tier is pooling across stores, and pooling is only sound
if the contributions are homogeneous, consented, and broadly sourced. This tool
mechanizes that check. It never collects: summaries are exchanged by hand as
attested artifacts under written consent (see benchmarks/README.md).

Inputs (all under the operator's control, none fetched):

  --manifest <json>   A manifest listing contributions. Each contribution pairs
                      a summary JSON path with a consent-record JSON path:
                        {"contributions": [
                           {"summary": "a.json", "consent": "a.consent.json"},
                           ...]}
  --pooling-operator  Identity of the operator running the pool (needed for the
                      dual-attestation gate).
  --json-out <path>   Write the machine report here; human-readable stdout
                      otherwise.

A consent record is a JSON object:
  {"operator": ..., "attestor": ..., "fingerprint_hash": <sha256 hex over the
   canonical JSON of the summary's fingerprint>, "date": ..., "scope": ...,
   "revocation_terms": ...}

Stratification key: (firing class, battery_hash, bench major version, g version).
Contributions only pool within a stratum; a mismatched battery or bench major
lands in a different stratum, never blended.

Verdicts (per stratum): HETEROGENEOUS (the stores disagree; no pooled claim, and
no post-hoc re-stratification to rescue it), CERTIFIED (every pre-registered
gate clears), or UNATTAINABLE (stay dark). The pre-registered floors live in the
private constants block below and are NEVER echoed into any output -- reports
carry booleans and measured bounds only, mirroring retrieval_bench.py.

Usage:
    python benchmarks/pool_certify.py --manifest pool.json --pooling-operator me
    python benchmarks/pool_certify.py --manifest pool.json --pooling-operator me \
        --json-out report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from _stats import (
    beta_binomial_lower,
    clopper_pearson_lower,
    holm_adjusted,
    homogeneity_p,
    wilson_lower,
)

POOL_CERTIFY_VERSION = "1"

# ---------------------------------------------------------------------------
# Pre-registered certification constants. PRIVATE and pinned in code: they are
# the bar the pooled claim must clear, fixed before any pool is assembled, and
# they are NEVER written into a report. Only the pass/fail booleans and the
# measured bounds cross into any output -- the same posture retrieval_bench.py
# holds for its feasibility floors, so a report can be shared without leaking
# the threshold a future baseline reads against.
# ---------------------------------------------------------------------------

# Surfacing-precision floor: the developer alert-fatigue floor (pooled + LOSO).
_PRECISION_LB_FLOOR = 0.90
# Minimum coverage (fired / candidate-conflicts) lower bound the pool must hold.
_C_MIN = 0.30
# Maximum exposure (wrong-flags per artifact reviewed) the trust budget allows.
_E_MAX = 0.05
# Family-wise alpha for the homogeneity test (Holm-adjusted across strata).
_ALPHA = 0.05
# Minimum pooled n_fired before any pooled precision claim is admissible.
_MIN_POOLED_FIRED = 30
# Breadth floor: independent sourcing the pooled claim must rest on.
_MIN_STORES = 3
_MIN_OPERATORS = 3
_MIN_ATTESTORS = 2
# Pinned bench-major compatibility range: only these majors pool together.
_BENCH_COMPAT_MAJORS = frozenset({"1"})

# The firing classes a summary reports; each seeds its own stratum.
_FIRING_CLASSES = ("forward", "reverse_only", "cross_vocabulary")

# Fingerprint keys a v2 summary must carry to be poolable, including the
# operator-attested certification record.
_REQUIRED_FINGERPRINT_KEYS = (
    "bench_version",
    "battery_hash",
    "last_attested_date",
    "last_attested_operator",
)


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def fingerprint_hash(summary: dict) -> str:
    """SHA-256 over the canonical JSON of the summary's fingerprint.

    The store identity used for breadth counting and the value a consent record
    hash-links against. Deterministic key ordering so two operators hashing the
    same fingerprint agree.
    """
    return hashlib.sha256(_canonical(summary["fingerprint"]).encode("utf-8")).hexdigest()


def _bench_major(bench_version: str) -> str:
    return str(bench_version).split(".")[0]


def _g_version(fingerprint: dict) -> str:
    """The g-predicate structural version for stratification.

    Read from ``fingerprint.g_version`` when the summary carries one; otherwise
    derived from the bench major version, since g's structure is versioned in
    lockstep with the benchmark until it is cut loose on its own tag.
    """
    explicit = fingerprint.get("g_version")
    if explicit:
        return str(explicit)
    return _bench_major(fingerprint.get("bench_version", ""))


def admit(summary: dict, consent: dict) -> str | None:
    """Mechanical admissibility check. Returns None if admissible, else a reason.

    All gates are structural and must pass for the contribution to enter a
    stratum. Every rejection carries a message a human can act on.
    """
    if summary.get("summary_schema_version") != "2":
        got = summary.get("summary_schema_version")
        return f"summary_schema_version is {got!r}, not '2' (v1 summaries are not certifiable)"
    fp = summary.get("fingerprint")
    if not isinstance(fp, dict):
        return "summary is missing its fingerprint object"
    for key in _REQUIRED_FINGERPRINT_KEYS:
        if fp.get(key) in (None, ""):
            return f"fingerprint is incomplete: {key!r} is missing or empty"
    top_battery = summary.get("battery_hash")
    if not top_battery or top_battery != fp.get("battery_hash"):
        return "battery_hash missing or inconsistent between summary and fingerprint"
    if _bench_major(fp["bench_version"]) not in _BENCH_COMPAT_MAJORS:
        return f"bench_version {fp['bench_version']!r} is outside the pinned compatibility range"
    for key in ("operator", "attestor", "fingerprint_hash"):
        if not consent.get(key):
            return f"consent record is missing {key!r}"
    if consent["fingerprint_hash"] != fingerprint_hash(summary):
        return "consent record is not hash-linked to the summary fingerprint"
    art = summary.get("artifact_regime")
    if not isinstance(art, dict) or "per_class_cell_sizes" not in art or "exposure" not in art:
        # A structure-only run emits a v2 summary with no artifact regime; it
        # carries no cells to pool and cannot enter a stratum.
        return "summary carries no artifact_regime measurement to pool"
    return None


def _stratum_key(firing_class: str, summary: dict) -> tuple[str, str, str, str]:
    fp = summary["fingerprint"]
    return (
        firing_class,
        fp["battery_hash"],
        _bench_major(fp["bench_version"]),
        _g_version(fp),
    )


def _cp_lower_over(members: list[dict]) -> float:
    k = sum(m["surfacing_hits"] for m in members)
    n = sum(m["fired"] for m in members)
    return clopper_pearson_lower(k, n)


def _loso_min_cp_lower(members: list[dict]) -> float:
    """Minimum pooled Clopper-Pearson lower bound over leave-one-store-out pools.

    Distinct stores (by fingerprint hash) are dropped one at a time; the pooled
    CP lower bound is recomputed on each remainder and the minimum is returned.
    A pool that only clears the floor because one generous store carries it fails
    here. Returns 0.0 when fewer than two stores leave nothing to hold out.
    """
    by_store: dict[str, list[dict]] = {}
    for m in members:
        by_store.setdefault(m["store"], []).append(m)
    stores = list(by_store)
    if len(stores) < 2:
        return 0.0
    worst = 1.0
    for dropped in stores:
        remainder = [m for s in stores if s != dropped for m in by_store[s]]
        worst = min(worst, _cp_lower_over(remainder))
    return worst


def _build_stratum_report(
    key: tuple[str, str, str, str],
    members: list[dict],
    homogeneity_raw: float,
    homogeneity_adj: float,
    pooling_operator: str | None,
) -> dict:
    firing_class, battery_hash, bench_major, g_version = key

    pooled_k = sum(m["surfacing_hits"] for m in members)
    pooled_n = sum(m["fired"] for m in members)
    coverage_k = sum(m["fired"] for m in members)
    coverage_n = sum(m["candidate"] for m in members)

    stores = sorted({m["store"] for m in members})
    operators = sorted({m["operator"] for m in members})
    attestors = sorted({m["attestor"] for m in members})

    exposure_wrong = sum(m["wrong_fires"] for m in members)
    exposure_reviewed = sum(m["artifacts_reviewed"] for m in members)
    exposure_rate = exposure_wrong / exposure_reviewed if exposure_reviewed else None

    cp_lower = clopper_pearson_lower(pooled_k, pooled_n)
    coverage_cp_lower = clopper_pearson_lower(coverage_k, coverage_n)
    loso_min = _loso_min_cp_lower(members)
    bb_lower = beta_binomial_lower([(m["surfacing_hits"], m["fired"]) for m in members])

    homogeneous = homogeneity_adj > _ALPHA
    dual_attestation = pooling_operator is not None and any(
        op != pooling_operator for op in operators
    )
    checks = {
        "homogeneous": homogeneous,
        "cp_lower_ge_floor": cp_lower >= _PRECISION_LB_FLOOR,
        "coverage_lower_ge_c_min": coverage_cp_lower >= _C_MIN,
        "exposure_le_e_max": exposure_rate is not None and exposure_rate <= _E_MAX,
        "pooled_fired_ge_min": pooled_n >= _MIN_POOLED_FIRED,
        "breadth_stores": len(stores) >= _MIN_STORES,
        "breadth_operators": len(operators) >= _MIN_OPERATORS,
        "breadth_attestors": len(attestors) >= _MIN_ATTESTORS,
        "dual_attestation": dual_attestation,
        "loso_min_ge_floor": loso_min >= _PRECISION_LB_FLOOR,
    }

    if not homogeneous:
        verdict = "HETEROGENEOUS"
    elif all(checks.values()):
        verdict = "CERTIFIED"
    else:
        verdict = "UNATTAINABLE"

    return {
        "firing_class": firing_class,
        "battery_hash": battery_hash,
        "bench_major": bench_major,
        "g_version": g_version,
        "pooled": {"k": pooled_k, "n": pooled_n},
        "cp_lower": round(cp_lower, 4),
        "wilson_lower": round(wilson_lower(pooled_k, pooled_n), 4),
        "coverage": {
            "k": coverage_k,
            "n": coverage_n,
            "cp_lower": round(coverage_cp_lower, 4),
            "wilson_lower": round(wilson_lower(coverage_k, coverage_n), 4),
        },
        "exposure_rate": round(exposure_rate, 4) if exposure_rate is not None else None,
        "homogeneity_p": round(homogeneity_raw, 6),
        "homogeneity_p_adjusted": round(homogeneity_adj, 6),
        "loso_min_cp_lower": round(loso_min, 4),
        # Reported sensitivity read only; the verdict never reads this.
        "beta_binomial_lower": round(bb_lower, 4),
        "n_stores": len(stores),
        "n_operators": len(operators),
        "n_attestors": len(attestors),
        "fingerprint_hashes": stores,
        "checks": checks,
        "verdict": verdict,
    }


def build_report(contributions: list[dict], pooling_operator: str | None = None) -> dict:
    """Pool and certify a list of loaded contributions.

    Each contribution is a dict ``{"summary": <dict>, "consent": <dict>}`` with
    an optional ``"source"`` label for reporting. Runs admissibility, stratifies
    the admissible ones, computes each stratum, and applies the Holm correction
    to the family of homogeneity p-values across strata before the verdicts.

    Structure-only by design: takes already-loaded dicts so callers (and the
    CI smoke) can pool fabricated counts without any file or store I/O.
    """
    rejected: list[dict] = []
    strata: dict[tuple[str, str, str, str], list[dict]] = {}

    for contribution in contributions:
        summary = contribution["summary"]
        consent = contribution["consent"]
        source = contribution.get("source")
        reason = admit(summary, consent)
        if reason is not None:
            rejected.append({"source": source, "reason": reason})
            continue
        store = fingerprint_hash(summary)
        cells = summary["artifact_regime"]["per_class_cell_sizes"]
        exposure = summary["artifact_regime"]["exposure"]
        for firing_class in _FIRING_CLASSES:
            cell = cells.get(firing_class)
            if cell is None:
                continue
            member = {
                "store": store,
                "operator": consent["operator"],
                "attestor": consent["attestor"],
                "surfacing_hits": cell["surfacing_hits"],
                "fired": cell["fired"],
                "candidate": cell["candidate"],
                "wrong_fires": exposure["wrong_fires"],
                "artifacts_reviewed": exposure["artifacts_reviewed"],
            }
            strata.setdefault(_stratum_key(firing_class, summary), []).append(member)

    ordered_keys = sorted(strata)
    raw_p = [
        homogeneity_p([(m["surfacing_hits"], m["fired"]) for m in strata[key]])
        for key in ordered_keys
    ]
    adj_p = holm_adjusted(raw_p)

    stratum_reports = [
        _build_stratum_report(key, strata[key], raw, adj, pooling_operator)
        for key, raw, adj in zip(ordered_keys, raw_p, adj_p, strict=True)
    ]

    return {
        "pool_certify_version": POOL_CERTIFY_VERSION,
        "n_contributions": len(contributions),
        "n_admissible": len(contributions) - len(rejected),
        "rejected": rejected,
        "strata": stratum_reports,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_contributions(manifest_path: Path) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("contributions", [])
    base = manifest_path.parent
    contributions: list[dict] = []
    for entry in entries:
        summary_path = (base / entry["summary"]).expanduser()
        consent_path = (base / entry["consent"]).expanduser()
        contributions.append(
            {
                "summary": json.loads(summary_path.read_text(encoding="utf-8")),
                "consent": json.loads(consent_path.read_text(encoding="utf-8")),
                "source": entry["summary"],
            }
        )
    return contributions


def print_report(report: dict) -> None:
    print(f"pool_certify v{report['pool_certify_version']}")
    print(
        f"contributions: {report['n_contributions']} "
        f"({report['n_admissible']} admissible, {len(report['rejected'])} rejected)"
    )
    for row in report["rejected"]:
        print(f"  rejected {row['source']!r}: {row['reason']}")
    if not report["strata"]:
        print("no admissible strata to certify")
        return
    for st in report["strata"]:
        print()
        print(
            f"stratum: class={st['firing_class']} battery={st['battery_hash']} "
            f"bench-major={st['bench_major']} g={st['g_version']}"
        )
        print(
            f"  pooled surfacing {st['pooled']['k']}/{st['pooled']['n']} "
            f"(CP-LB={st['cp_lower']}, Wilson-LB={st['wilson_lower']})"
        )
        print(
            f"  coverage {st['coverage']['k']}/{st['coverage']['n']} "
            f"(CP-LB={st['coverage']['cp_lower']}); exposure rate={st['exposure_rate']}"
        )
        print(
            f"  homogeneity p={st['homogeneity_p']} "
            f"(Holm-adjusted {st['homogeneity_p_adjusted']}); "
            f"LOSO min CP-LB={st['loso_min_cp_lower']}; "
            f"beta-binomial LB={st['beta_binomial_lower']} (sensitivity, non-gating)"
        )
        print(
            f"  breadth: {st['n_stores']} stores, {st['n_operators']} operators, "
            f"{st['n_attestors']} attestors"
        )
        failing = [name for name, ok in st["checks"].items() if not ok]
        print(f"  VERDICT: {st['verdict']}")
        if st["verdict"] != "CERTIFIED" and failing:
            print(f"    failing checks: {', '.join(failing)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Manifest JSON listing (summary, consent) contribution pairs",
    )
    parser.add_argument(
        "--pooling-operator",
        help="Identity of the operator running the pool (dual-attestation gate)",
    )
    parser.add_argument("--json-out", type=Path, help="Write the machine report JSON here")
    args = parser.parse_args(argv)

    contributions = _load_contributions(args.manifest.expanduser())
    report = build_report(contributions, pooling_operator=args.pooling_operator)

    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json_out}", file=sys.stderr)
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
