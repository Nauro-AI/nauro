"""Structural smoke for benchmarks/pool_certify.py and the new _stats primitives.

Scope is deliberately structure-only, on SYNTHETIC fabricated counts. No fixture
is store-derived and nothing here asserts a precision/catch/score number: the
verdict logic and the admissibility gates are mechanical, so this pins their
structure (schema, the three-value verdict set, breadth and homogeneity routing,
the admissibility rejections) without gating retrieval quality.

The counts are hand-built to exercise each path: a certifiable homogeneous
large-n three-store pool, a breadth-short two-store pool, a wildly heterogeneous
pool, a consent-hash mismatch, and a v1 summary. The _stats checks are
monotonicity / range properties, not tuned values.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[3] / "benchmarks"
# pool_certify.py imports its sibling _stats module; a direct script run resolves
# that via sys.path[0]. Under pytest the loader needs benchmarks/ on sys.path.
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BENCH_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Synthetic fixture builders (fabricated counts only, never store-derived).
# ---------------------------------------------------------------------------


def _summary(
    store_seed: str,
    cells: dict[str, tuple[int, int, int]],
    *,
    exposure: dict | None = None,
    schema: str = "2",
    battery: str = "batt-1",
    bench_version: str = "1",
    attested_date: str = "2026-07-01",
    attested_operator: str = "att-op",
) -> dict:
    """A fabricated schema-v2 summary. ``cells`` maps class -> (hits, fired, candidate)."""
    fingerprint = {
        "bench_version": bench_version,
        "battery_hash": battery,
        "last_attested_date": attested_date,
        "last_attested_operator": attested_operator,
        # Distinguishes store identity so distinct seeds hash to distinct stores.
        "store_seed": store_seed,
    }
    if exposure is None:
        exposure = {"wrong_fires": 0, "artifacts_reviewed": 200, "rate": 0.0}
    per_class = {
        klass: {"candidate": candidate, "fired": fired, "surfacing_hits": hits}
        for klass, (hits, fired, candidate) in cells.items()
    }
    return {
        "summary_schema_version": schema,
        "fingerprint": fingerprint,
        "battery_hash": battery,
        "artifact_regime": {
            "per_class_cell_sizes": per_class,
            "exposure": exposure,
        },
    }


def _consent(pc, summary: dict, operator: str, attestor: str, *, valid_hash: bool = True) -> dict:
    fp_hash = pc.fingerprint_hash(summary) if valid_hash else "0" * 64
    return {
        "operator": operator,
        "attestor": attestor,
        "fingerprint_hash": fp_hash,
        "date": "2026-07-01",
        "scope": "surfacing precision pooling",
        "revocation_terms": "revocable on written notice",
    }


def _contribution(
    pc,
    store_seed: str,
    operator: str,
    attestor: str,
    cells: dict[str, tuple[int, int, int]],
    *,
    valid_hash: bool = True,
    **summary_kwargs,
) -> dict:
    summary = _summary(store_seed, cells, **summary_kwargs)
    consent = _consent(pc, summary, operator, attestor, valid_hash=valid_hash)
    return {"summary": summary, "consent": consent, "source": store_seed}


def _certifiable_pool(pc) -> list[dict]:
    """Three homogeneous high-precision large-n stores that clear every gate."""
    cells = {"forward": (49, 50, 60)}  # hits, fired, candidate -> precision 49/50
    exposure = {"wrong_fires": 1, "artifacts_reviewed": 200, "rate": 0.005}
    return [
        _contribution(pc, "s1", "op-a", "att-x", cells, exposure=exposure),
        _contribution(pc, "s2", "op-b", "att-y", cells, exposure=exposure),
        _contribution(pc, "s3", "op-c", "att-x", cells, exposure=exposure),
    ]


_STRATUM_KEYS = {
    "firing_class",
    "battery_hash",
    "bench_major",
    "g_version",
    "pooled",
    "cp_lower",
    "wilson_lower",
    "coverage",
    "exposure_rate",
    "homogeneity_p",
    "homogeneity_p_adjusted",
    "loso_min_cp_lower",
    "beta_binomial_lower",
    "n_stores",
    "n_operators",
    "n_attestors",
    "fingerprint_hashes",
    "checks",
    "verdict",
}
_VERDICTS = {"CERTIFIED", "HETEROGENEOUS", "UNATTAINABLE"}


# ---------------------------------------------------------------------------
# Report structure and verdict routing.
# ---------------------------------------------------------------------------


def test_report_is_schema_valid_and_verdicts_bounded() -> None:
    pc = _load("pool_certify")
    report = pc.build_report(_certifiable_pool(pc), pooling_operator="op-pooler")

    assert set(report) == {
        "pool_certify_version",
        "n_contributions",
        "n_admissible",
        "rejected",
        "strata",
    }
    assert report["n_contributions"] == 3
    assert report["n_admissible"] == 3
    assert report["rejected"] == []
    assert report["strata"], "expected at least one stratum"
    for st in report["strata"]:
        assert set(st) == _STRATUM_KEYS
        assert st["verdict"] in _VERDICTS
        assert isinstance(st["checks"], dict)
        assert all(isinstance(v, bool) for v in st["checks"].values())


def test_homogeneous_large_n_three_store_pool_certifies() -> None:
    pc = _load("pool_certify")
    report = pc.build_report(_certifiable_pool(pc), pooling_operator="op-pooler")
    forward = next(st for st in report["strata"] if st["firing_class"] == "forward")
    assert forward["verdict"] == "CERTIFIED", forward["checks"]
    assert forward["n_stores"] == 3
    assert forward["n_operators"] == 3
    assert forward["n_attestors"] == 2


def test_two_store_pool_fails_breadth_floor() -> None:
    pc = _load("pool_certify")
    report = pc.build_report(_certifiable_pool(pc)[:2], pooling_operator="op-pooler")
    forward = next(st for st in report["strata"] if st["firing_class"] == "forward")
    assert forward["verdict"] != "CERTIFIED"
    assert forward["verdict"] == "UNATTAINABLE"
    assert forward["checks"]["breadth_stores"] is False


def test_heterogeneous_pool_is_flagged() -> None:
    pc = _load("pool_certify")
    exposure = {"wrong_fires": 1, "artifacts_reviewed": 200, "rate": 0.005}
    contribs = [
        _contribution(pc, "s1", "op-a", "att-x", {"forward": (50, 50, 60)}, exposure=exposure),
        _contribution(pc, "s2", "op-b", "att-y", {"forward": (25, 50, 60)}, exposure=exposure),
        _contribution(pc, "s3", "op-c", "att-x", {"forward": (5, 50, 60)}, exposure=exposure),
    ]
    report = pc.build_report(contribs, pooling_operator="op-pooler")
    forward = next(st for st in report["strata"] if st["firing_class"] == "forward")
    assert forward["verdict"] == "HETEROGENEOUS"
    assert forward["checks"]["homogeneous"] is False


def test_consent_hash_mismatch_is_rejected() -> None:
    pc = _load("pool_certify")
    contribs = [
        _contribution(pc, "s1", "op-a", "att-x", {"forward": (49, 50, 60)}, valid_hash=False),
    ]
    report = pc.build_report(contribs, pooling_operator="op-pooler")
    assert report["n_admissible"] == 0
    assert report["strata"] == []
    assert len(report["rejected"]) == 1
    assert "hash-linked" in report["rejected"][0]["reason"]


def test_v1_summary_is_rejected() -> None:
    pc = _load("pool_certify")
    contribs = [
        _contribution(pc, "s1", "op-a", "att-x", {"forward": (49, 50, 60)}, schema="1"),
    ]
    report = pc.build_report(contribs, pooling_operator="op-pooler")
    assert report["n_admissible"] == 0
    assert report["strata"] == []
    assert len(report["rejected"]) == 1
    assert "v1" in report["rejected"][0]["reason"]


def test_bench_version_out_of_range_is_rejected() -> None:
    pc = _load("pool_certify")
    contribs = [
        _contribution(pc, "s1", "op-a", "att-x", {"forward": (49, 50, 60)}, bench_version="99"),
    ]
    report = pc.build_report(contribs, pooling_operator="op-pooler")
    assert report["n_admissible"] == 0
    assert "compatibility range" in report["rejected"][0]["reason"]


def test_beta_binomial_never_gates_verdict() -> None:
    """The certifier reports the beta-binomial bound but never reads it in checks."""
    pc = _load("pool_certify")
    report = pc.build_report(_certifiable_pool(pc), pooling_operator="op-pooler")
    forward = next(st for st in report["strata"] if st["firing_class"] == "forward")
    assert "beta_binomial_lower" in forward
    assert "beta_binomial" not in forward["checks"]


# ---------------------------------------------------------------------------
# _stats primitive properties (monotonicity / range, not tuned values).
# ---------------------------------------------------------------------------


def test_homogeneity_p_range_and_ordering() -> None:
    st = _load("_stats")
    homogeneous = [(49, 50), (49, 50), (48, 50)]
    heterogeneous = [(50, 50), (25, 50), (5, 50)]
    p_hom = st.homogeneity_p(homogeneous)
    p_het = st.homogeneity_p(heterogeneous)
    for p in (p_hom, p_het):
        assert 0.0 <= p <= 1.0
    assert p_het < p_hom
    # Nothing to test with fewer than two informative stores.
    assert st.homogeneity_p([(5, 10)]) == 1.0
    assert st.homogeneity_p([]) == 1.0


def test_homogeneity_exact_path_small_counts() -> None:
    st = _load("_stats")
    p_hom = st.homogeneity_p([(3, 3), (3, 3), (2, 3)])
    p_het = st.homogeneity_p([(3, 3), (0, 3), (3, 3)])
    assert 0.0 <= p_het <= p_hom <= 1.0
    assert p_het < p_hom


def test_beta_binomial_lower_below_pooled_point_estimate() -> None:
    st = _load("_stats")
    for counts in ([(49, 50), (48, 50), (49, 50)], [(50, 50), (25, 50), (5, 50)]):
        k = sum(a for a, _ in counts)
        n = sum(b for _, b in counts)
        point = k / n
        bb = st.beta_binomial_lower(counts)
        assert 0.0 <= bb <= point
        # Overdispersion only widens the interval: never above the fixed-effect bound.
        assert bb <= st.wilson_lower(k, n) + 1e-12


def test_holm_adjusted_is_monotone_and_bounded() -> None:
    st = _load("_stats")
    adjusted = st.holm_adjusted([0.001, 0.02, 0.5])
    assert all(0.0 <= p <= 1.0 for p in adjusted)
    # Aligned with input order; the smallest raw p carries the family multiplier.
    assert adjusted[0] >= 0.001
    assert st.holm_adjusted([]) == []
