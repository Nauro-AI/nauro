# Retrieval benchmark

`retrieval_bench.py` measures the production retrieval kernel on a store's own
supersession history: for each decision that replaced another, would retrieval
have surfaced the replaced decision in the top-K an agent reads, at the moment
the replacement was filed? A Nauro store generates this ground truth as a
byproduct of normal use, so any store benchmarks itself.

Run it before merging retrieval changes and cite the numbers in the PR's test
plan. It is developer tooling: not shipped in any wheel, not a quality gate.

## Usage

```bash
uv run python benchmarks/retrieval_bench.py --store ~/.nauro/projects/<project-id>
uv run python benchmarks/retrieval_bench.py --store PATH --embeddings        # union arm
uv run python benchmarks/retrieval_bench.py --store PATH --json-out run.json
uv run python benchmarks/retrieval_bench.py --store PATH --baseline prev.json
```

## What it reports

- **Conflict catching** per query regime (terse title; title plus first
  sentence), per event class. Forward events (`supersedes` edges) and
  reverse-only events (`superseded_by` edges with no forward link, which is
  where one-to-many retirements live because `supersedes` is single-valued)
  report separately and are never blended: an N-member retirement cannot fit a
  K-slot list, so reverse-only rates are slot-bounded by arithmetic.
- **Novel battery**: top-1 score band for a fixed set of generic engineering
  proposals the store has not decided. A recall change that inflates this band
  is buying catches with noise. Rot mode: a query stops being novel the day
  the project decides that topic; review the battery when the band shifts
  without a retrieval change. Amendments change the battery hash carried in
  every output.
- **Off-domain abstain**: queries far outside any decision store's domain,
  expected to return zero results under the production score cutoff.
  Reported, not asserted.

## Reading the numbers

Catch rates are corpus-relative: they shift with corpus growth, dependency
resolution, and battery membership, not only with code changes. Compare runs
only through `--baseline`, which diffs the corpus and configuration
fingerprint first and warns on any mismatch.

## CI

CI runs a structural smoke against the generated demo store
(`packages/nauro/tests/test_retrieval_bench_smoke.py`): the script completes,
derives the expected event count including the demo's reverse-only
consolidation fan, and emits schema-valid JSON. It asserts nothing about
catch, rank, or score; green CI means the tool runs, not that retrieval
quality is verified.

## Cross-store pooling

A single store is years from the high-stakes surfacing lower bound; the only
path to the emit tier is pooling the measurement across independently operated
stores. `pool_certify.py` does that pooling and certification, stdlib-only:

```bash
python benchmarks/pool_certify.py --manifest pool.json --pooling-operator me
python benchmarks/pool_certify.py --manifest pool.json --pooling-operator me \
    --json-out report.json
```

Its inputs are the privacy-preserving schema-v2 summaries that
`retrieval_bench.py --summary-out` emits (counts and bounds only, never store
text). The manifest lists contributions, each pairing a summary JSON with a
consent record (operator, attestor, a hash linking the consent to the summary's
fingerprint, date, scope, revocation terms). Contributions pool only within a
stratum keyed on firing class, battery hash, bench major version, and g version.

Each stratum lands on one of three verdicts:

- **CERTIFIED** — every pre-registered gate clears (pooled and leave-one-store-out
  precision lower bounds, coverage, exposure, minimum pooled fires, a breadth
  floor on stores/operators/attestors, and dual attestation).
- **HETEROGENEOUS** — the stores disagree (the homogeneity test rejects at the
  Holm-adjusted alpha); there is no pooled claim and no post-hoc re-stratification
  to rescue it.
- **UNATTAINABLE** — stay dark; the bar is not met on this pool.

The pre-registered floors live pinned in code and are never echoed into a report;
reports carry booleans and measured bounds only.

## Data posture

The tool and its generic batteries are public. Anything derived from a real
store (conflict events, paraphrases of decisions, replayed traffic) stays
private with the store. No store content is ever committed here.

No collection is authorized. `pool_certify.py` never fetches anything: summaries
are exchanged by hand as attested artifacts under written consent, and pooling
runs only over what an operator was given directly.
