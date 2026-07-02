"""Tests for ``nauro hook user-prompt-submit`` — the advisory Claude Code hook.

The hook is advisory by construction: it must never block a turn, so every
failure path is asserted to exit 0 with empty stdout. Project resolution,
relevance-floor suppression, and per-session dedup are exercised against an
isolated ``NAURO_HOME`` store seeded with real-shaped decision files.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from nauro_core.decision_model import Decision, format_decision
from typer.testing import CliRunner

from nauro.cli.commands import hook
from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _write_decision(
    store_path: Path,
    *,
    num: int,
    title: str,
    rationale: str,
    status: str = "active",
    supersedes: str | None = None,
    superseded_by: str | None = None,
) -> None:
    """Write a valid v2 decision file into the store's decisions directory."""
    decision = Decision(
        date=dt.date(2026, 5, 1),
        confidence="medium",
        status=status,
        num=num,
        title=title,
        rationale=rationale,
        supersedes=supersedes,
        superseded_by=superseded_by,
    )
    content = format_decision(decision)
    slug = title.lower().replace(" ", "-")[:40]
    (store_path / "decisions" / f"{num:03d}-{slug}.md").write_text(content)


# BM25 IDF — and therefore the relevance-floor score — scales with corpus
# size. A tiny store scores a strong match well below the production floor, an
# artifact of corpus size rather than relevance. Seed a corpus of unrelated
# distractor decisions so scoring approximates a real store and a genuine match
# clears RELEVANCE_FLOOR the way it does in the field.
_DISTRACTOR_TOPICS = [
    "billing",
    "authentication",
    "logging",
    "caching",
    "deployment",
    "metrics",
    "search",
    "synchronization",
    "configuration",
    "schema",
    "telemetry",
    "registry",
    "snapshot",
    "migration",
    "templating",
    "cursor",
    "codex",
    "embedding",
    "retrieval",
    "parser",
    "validator",
    "session",
    "questions",
    "import",
    "export",
    "backup",
    "throttle",
    "webhook",
    "queue",
    "indexing",
]


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """Register a project rooted at a fresh repo dir; return (repo, store_path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store_path = register_project_v2("hookproj", [repo])
    scaffold_project_store("hookproj", store_path)
    for i, topic in enumerate(_DISTRACTOR_TOPICS, start=100):
        _write_decision(
            store_path,
            num=i,
            title=f"approach for the {topic} subsystem",
            rationale=f"the {topic} subsystem uses a dedicated module with isolated state",
        )
    return repo, store_path


def _invoke(payload: dict):
    return runner.invoke(
        app,
        ["hook", "user-prompt-submit"],
        input=json.dumps(payload),
    )


# ── stdin parsing ─────────────────────────────────────────────────────────────


def test_malformed_json_exits_zero_empty(tmp_path: Path):
    """Malformed stdin JSON fails open: exit 0, no stdout."""
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input="{not json")
    assert result.exit_code == 0
    assert result.output == ""


def test_missing_prompt_exits_zero_empty(tmp_path: Path):
    """A payload without a prompt key fails open: exit 0, no stdout."""
    repo, _store = _make_project(tmp_path)
    result = _invoke({"cwd": str(repo), "session_id": "s1"})
    assert result.exit_code == 0
    assert result.output == ""


def test_empty_prompt_exits_zero_empty(tmp_path: Path):
    """A blank prompt fails open: exit 0, no stdout."""
    repo, _store = _make_project(tmp_path)
    result = _invoke({"prompt": "   ", "cwd": str(repo), "session_id": "s1"})
    assert result.exit_code == 0
    assert result.output == ""


# ── fail-open on resolution / store problems ───────────────────────────────────


def test_no_project_for_cwd_exits_zero_empty(tmp_path: Path):
    """An unregistered cwd resolves to no project: exit 0, no stdout."""
    unregistered = tmp_path / "elsewhere"
    unregistered.mkdir()
    result = _invoke(
        {"prompt": "switch the database to postgres", "cwd": str(unregistered), "session_id": "s1"}
    )
    assert result.exit_code == 0
    assert result.output == ""


def test_missing_store_exits_zero_empty(tmp_path: Path):
    """A registered repo whose store dir was removed fails open: exit 0, empty."""
    repo, store_path = _make_project(tmp_path)
    # Remove the store directory the registry points at.
    for child in sorted(store_path.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    store_path.rmdir()
    result = _invoke({"prompt": "rework the auth layer", "cwd": str(repo), "session_id": "s1"})
    assert result.exit_code == 0
    assert result.output == ""


def test_oversize_prompt_exits_zero_empty(tmp_path: Path):
    """An over-length prompt trips the kernel's rejection path: exit 0, empty."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=10,
        title="adopt postgres for the primary datastore",
        rationale="postgres chosen for relational integrity and operational maturity",
    )
    # MAX_APPROACH_LENGTH is in the low thousands; 50k chars trips rejection.
    result = _invoke({"prompt": "x" * 50_000, "cwd": str(repo), "session_id": "s1"})
    assert result.exit_code == 0
    assert result.output == ""


# ── the success envelope ───────────────────────────────────────────────────────


def test_valid_hook_output_envelope(tmp_path: Path):
    """A relevant prompt yields a valid hookSpecificOutput envelope."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=12,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen over dynamodb for relational integrity and SQL tooling",
    )
    result = _invoke(
        {
            "prompt": "should we adopt postgres as the primary datastore for the service",
            "cwd": str(repo),
            "session_id": "envelope",
        }
    )
    assert result.exit_code == 0
    assert result.output != ""
    payload = json.loads(result.output)
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "D012" in out["additionalContext"]
    assert "postgres" in out["additionalContext"]
    # Advisory framing + the review instruction are both present.
    assert "advisory" in out["additionalContext"].lower()
    assert "get_decision" in out["additionalContext"]


# ── relevance-floor suppression ────────────────────────────────────────────────


def test_no_hits_above_floor_suppressed(tmp_path: Path):
    """A prompt with no decision above the BM25 floor injects nothing."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=20,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen for relational integrity",
    )
    # A prompt sharing no vocabulary with any decision.
    result = _invoke(
        {
            "prompt": "draw a purple butterfly on the canvas widget",
            "cwd": str(repo),
            "session_id": "floor",
        }
    )
    assert result.exit_code == 0
    assert result.output == ""


# ── per-session dedup ──────────────────────────────────────────────────────────


def test_per_session_dedup_does_not_resurface(tmp_path: Path):
    """Within one session, a decision surfaced once is not surfaced again."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=30,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen over dynamodb for relational integrity and SQL tooling",
    )
    payload = {
        "prompt": "should we adopt postgres as the primary datastore",
        "cwd": str(repo),
        "session_id": "dedup-session",
    }
    first = _invoke(payload)
    assert first.exit_code == 0
    assert "D030" in first.output

    second = _invoke(payload)
    assert second.exit_code == 0
    # Already surfaced this session — not re-injected.
    assert second.output == ""


# ── corpus-size-aware relevance floor ───────────────────────────────────────────


def test_effective_floor_scales_down_for_small_corpus():
    """A small corpus gets a reachable floor; at/above the reference it is full."""
    assert hook._effective_floor(hook.RELEVANCE_FLOOR_REFERENCE_CORPUS) == hook.RELEVANCE_FLOOR
    assert hook._effective_floor(1000) == hook.RELEVANCE_FLOOR
    demo_floor = hook._effective_floor(7)
    assert 0 < demo_floor < hook.RELEVANCE_FLOOR


def test_effective_floor_env_override(monkeypatch):
    monkeypatch.setenv(hook.RELEVANCE_FLOOR_ENV, "1.5")
    assert hook._effective_floor(7) == 1.5
    assert hook._effective_floor(1000) == 1.5
    # A non-numeric override is ignored, falling back to corpus scaling.
    monkeypatch.setenv(hook.RELEVANCE_FLOOR_ENV, "not-a-number")
    assert hook._effective_floor(1000) == hook.RELEVANCE_FLOOR


def test_demo_store_surfaces_conflict_without_corpus_padding(tmp_path: Path):
    """The 7-decision demo store must surface its marquee websocket→SSE conflict.

    Regression for the fixed 8.0 floor that no demo decision could reach (D004
    scores ~5), making --with-hooks look broken on the obvious demo path. No
    distractor padding here — the corpus-aware floor must do the work.
    """
    from nauro.demo import create_demo_project

    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store_path = register_project_v2("demoproj", [repo])
    create_demo_project(store_path)

    result = _invoke(
        {
            "prompt": "add a websocket endpoint for live task updates",
            "cwd": str(repo),
            "session_id": "demo",
        }
    )
    assert result.exit_code == 0
    assert result.output != ""
    out = json.loads(result.output)["hookSpecificOutput"]
    assert "D004" in out["additionalContext"]


def test_distinct_session_resurfaces(tmp_path: Path):
    """A different session id surfaces the same decision again."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=31,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen over dynamodb for relational integrity and SQL tooling",
    )
    base = {
        "prompt": "should we adopt postgres as the primary datastore",
        "cwd": str(repo),
    }
    first = _invoke({**base, "session_id": "session-a"})
    assert "D031" in first.output
    second = _invoke({**base, "session_id": "session-b"})
    assert "D031" in second.output


# ── embeddings flag on the invocation path ─────────────────────────────────────


def _patch_capturing_check(monkeypatch, captured: dict) -> None:
    """Patch the kernel's check_decision to capture the use_embeddings argument.

    The operations package re-exports ``check_decision`` at package level, which
    shadows the submodule of the same name; reach the real module via sys.modules
    so the patch lands on the binding ``hook._check`` imports.
    """
    import sys

    def fake_check(store, approach, use_embeddings=False):
        captured["use_embeddings"] = use_embeddings

        class _Empty:
            error = None
            related_decisions: list = []

        return _Empty()

    check_mod = sys.modules["nauro_core.operations.check_decision"]
    monkeypatch.setattr(check_mod, "check_decision", fake_check)


def test_mvp_runs_bm25_only(tmp_path: Path, monkeypatch):
    """With no NAURO_EMBEDDINGS in the environment, the hook runs BM25-only.

    The MVP install does not set the flag, so the resolved value passed to the
    kernel is False — embeddings are not engaged by default.
    """
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=40,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen for relational integrity",
    )
    monkeypatch.delenv("NAURO_EMBEDDINGS", raising=False)
    captured: dict = {}
    _patch_capturing_check(monkeypatch, captured)

    result = _invoke({"prompt": "adopt postgres", "cwd": str(repo), "session_id": "flag"})
    assert result.exit_code == 0
    assert captured["use_embeddings"] is False


def test_embeddings_flag_wiring_retained(tmp_path: Path, monkeypatch):
    """The flag wiring is retained: setting NAURO_EMBEDDINGS engages it.

    The MVP install leaves the flag unset, but the hook still resolves it, so the
    cosine-gated follow-up can flip the backend on without code changes here.
    """
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=41,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen for relational integrity",
    )
    monkeypatch.setenv("NAURO_EMBEDDINGS", "1")
    captured: dict = {}
    _patch_capturing_check(monkeypatch, captured)

    result = _invoke({"prompt": "adopt postgres", "cwd": str(repo), "session_id": "flag2"})
    assert result.exit_code == 0
    assert captured["use_embeddings"] is True


# ── dedup state is fail-open ───────────────────────────────────────────────────


def test_corrupt_dedup_state_injects_anyway(tmp_path: Path):
    """An unreadable session state file does not silence a genuine conflict."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=50,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen over dynamodb for relational integrity and SQL tooling",
    )
    state_dir = tmp_path / "hook-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "corrupt.json").write_text("{not valid json")

    result = _invoke(
        {
            "prompt": "should we adopt postgres as the primary datastore",
            "cwd": str(repo),
            "session_id": "corrupt",
        }
    )
    assert result.exit_code == 0
    assert "D050" in result.output


# ── direct unit coverage of the floor + block helpers ──────────────────────────


# These pin behaviour at the full floor, so they pass a reference-sized corpus
# (effective floor == RELEVANCE_FLOOR); corpus-size scaling is covered separately.
_REF_CORPUS = hook.RELEVANCE_FLOOR_REFERENCE_CORPUS


def test_apply_floor_drops_embedding_only_hits():
    """Embedding-only hits (score 0.0) are not admitted in the BM25-only MVP."""
    hits = [
        {"number": 1, "title": "a", "score": 0.0, "status": "active", "date": "", "preview": ""},
        {"number": 2, "title": "b", "score": 0.0, "status": "active", "date": "", "preview": ""},
    ]
    assert hook._apply_floor(hits, _REF_CORPUS) == []


def test_apply_floor_keeps_only_bm25_above_floor():
    """Only BM25 hits at or above the floor survive; an embedding-only hit drops."""
    above = hook.RELEVANCE_FLOOR + 1.0
    hits = [
        {"number": 1, "title": "a", "score": above, "status": "active", "date": "", "preview": ""},
        {"number": 2, "title": "b", "score": 0.0, "status": "active", "date": "", "preview": ""},
    ]
    surviving = hook._apply_floor(hits, _REF_CORPUS)
    assert [h["number"] for h in surviving] == [1]


def test_apply_floor_drops_bm25_below_floor():
    """A BM25 hit below the floor is dropped; no embedding-only hit present."""
    below = hook.RELEVANCE_FLOOR - 0.1
    hits = [
        {"number": 1, "title": "a", "score": below, "status": "active", "date": "", "preview": ""},
    ]
    assert hook._apply_floor(hits, _REF_CORPUS) == []


def test_format_block_shape():
    """The block carries the preamble, the D### line, and the instruction."""
    hits = [
        {
            "number": 7,
            "title": "adopt postgres",
            "score": 5.0,
            "status": "active",
            "date": "2026-05-01",
            "preview": "postgres chosen for relational integrity",
        }
    ]
    block = hook._format_block(hits)
    assert hook._PREAMBLE in block
    assert hook._INSTRUCTION in block
    assert 'D007 "adopt postgres" (active, 2026-05-01)' in block
    # No enrichment payload on the hit: no rejection-relation wording is emitted.
    assert "supersedes" not in block
    assert "in favor of" not in block


# ── explicit rejection wording for structural supersedes refs ──────────────────


def test_supersedes_ref_renders_explicit_rejection(tmp_path: Path):
    """A surfaced decision that supersedes another states the rejection explicitly."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=60,
        title="use dynamodb as the primary datastore",
        rationale="dynamodb chosen for the primary datastore key-value access",
        status="superseded",
        superseded_by="61",
    )
    _write_decision(
        store_path,
        num=61,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen over dynamodb for relational integrity and SQL tooling",
        supersedes="60",
    )
    result = _invoke(
        {
            "prompt": "should we adopt postgres as the primary datastore for the service",
            "cwd": str(repo),
            "session_id": "supersede",
        }
    )
    assert result.exit_code == 0
    assert result.output != ""
    ctx = json.loads(result.output)["hookSpecificOutput"]["additionalContext"]
    assert "D061" in ctx
    assert 'supersedes D060 "use dynamodb as the primary datastore"' in ctx
    assert (
        'rejected "use dynamodb as the primary datastore" '
        'in favor of "adopt postgres as the primary datastore"' in ctx
    )


def test_supersedes_nonexistent_target_fails_open(tmp_path: Path):
    """A supersedes ref to a missing decision degrades to today's shipped line."""
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=62,
        title="adopt redis for the cache tier layer",
        rationale="redis chosen for the cache tier layer",
        supersedes="999",
    )
    result = _invoke(
        {
            "prompt": "should we adopt redis for the cache tier layer",
            "cwd": str(repo),
            "session_id": "missing-target",
        }
    )
    assert result.exit_code == 0
    assert result.output != ""
    ctx = json.loads(result.output)["hookSpecificOutput"]["additionalContext"]
    assert "D062" in ctx
    # Fail-open: the shipped line renders, with no relation wording.
    assert (
        'D062 "adopt redis for the cache tier layer" (active, 2026-05-01) — '
        "redis chosen for the cache tier layer" in ctx
    )
    assert "supersedes" not in ctx
    assert "in favor of" not in ctx


def test_supersedes_unparseable_target_fails_open(tmp_path: Path):
    """A present-but-unparseable supersedes target degrades to the shipped line."""
    repo, store_path = _make_project(tmp_path)
    # A file that carries the 063 number prefix but does not parse as v2.
    (store_path / "decisions" / "063-broken.md").write_text("not a valid decision file at all")
    _write_decision(
        store_path,
        num=64,
        title="adopt kafka for the event stream bus",
        rationale="kafka chosen for the event stream bus",
        supersedes="63",
    )
    result = _invoke(
        {
            "prompt": "should we adopt kafka for the event stream bus",
            "cwd": str(repo),
            "session_id": "unparseable-target",
        }
    )
    assert result.exit_code == 0
    assert result.output != ""
    ctx = json.loads(result.output)["hookSpecificOutput"]["additionalContext"]
    assert "D064" in ctx
    assert "supersedes" not in ctx
    assert "in favor of" not in ctx


def test_three_injected_refs_block_bounded(tmp_path: Path):
    """Three surfaced decisions carrying refs stay within the injection cap."""
    repo, store_path = _make_project(tmp_path)
    for old_num, new_num in ((65, 75), (66, 76), (67, 77)):
        _write_decision(
            store_path,
            num=old_num,
            title=f"legacy datastore choice number {old_num}",
            rationale=f"legacy datastore option {old_num} retired",
            status="superseded",
            superseded_by=str(new_num),
        )
        _write_decision(
            store_path,
            num=new_num,
            title=f"adopt postgres as the primary datastore variant {new_num}",
            rationale=(
                "postgres chosen as the primary datastore for relational integrity, "
                "SQL tooling, mature operational story, and strong transactional "
                "guarantees across the whole service surface area"
            ),
            supersedes=str(old_num),
        )
    result = _invoke(
        {
            "prompt": "adopt postgres as the primary datastore for the service",
            "cwd": str(repo),
            "session_id": "three-refs",
        }
    )
    assert result.exit_code == 0
    assert result.output != ""
    ctx = json.loads(result.output)["hookSpecificOutput"]["additionalContext"]
    d_lines = [ln for ln in ctx.split("\n") if len(ln) >= 4 and ln[0] == "D" and ln[1:4].isdigit()]
    # The injection cap holds even when every hit carries a ref.
    assert 1 <= len(d_lines) <= hook.MAX_INJECTED
    # Every surfaced decision here supersedes another, so each line states it.
    for ln in d_lines:
        assert "in favor of" in ln


# ── unit coverage of the rejection-wording renderer ────────────────────────────


def test_format_block_line_byte_identical_without_ref():
    """A hit without an enrichment payload renders exactly today's block."""
    hit = {
        "number": 7,
        "title": "adopt postgres",
        "score": 5.0,
        "status": "active",
        "date": "2026-05-01",
        "preview": "postgres chosen for relational integrity",
    }
    expected = "\n".join(
        [
            hook._PREAMBLE,
            'D007 "adopt postgres" (active, 2026-05-01) — postgres chosen for relational integrity',
            hook._INSTRUCTION,
        ]
    )
    assert hook._format_block([hit]) == expected


def test_format_block_explicit_line_for_enriched_hit():
    """An enriched hit renders the explicit rejection line verbatim."""
    hit = {
        "number": 61,
        "title": "adopt postgres as the primary datastore",
        "score": 5.0,
        "status": "active",
        "date": "2026-05-01",
        "preview": "postgres chosen over dynamodb for relational integrity",
        "supersedes_ref": {"number": 60, "title": "use dynamodb as the primary datastore"},
    }
    expected_line = (
        'D061 "adopt postgres as the primary datastore" (active, 2026-05-01) — '
        'supersedes D060 "use dynamodb as the primary datastore": this decision '
        'rejected "use dynamodb as the primary datastore" in favor of '
        '"adopt postgres as the primary datastore". '
        "postgres chosen over dynamodb for relational integrity"
    )
    expected = "\n".join([hook._PREAMBLE, expected_line, hook._INSTRUCTION])
    assert hook._format_block([hit]) == expected


def test_explicit_line_snippet_respects_preview_cap():
    """The rationale snippet on an explicit line reuses the shared 120-char cap."""
    long_preview = "postgres chosen for relational integrity and operational maturity " * 10
    hit = {
        "number": 61,
        "title": "adopt postgres",
        "score": 5.0,
        "status": "active",
        "date": "2026-05-01",
        "preview": long_preview,
        "supersedes_ref": {"number": 60, "title": "use dynamodb"},
    }
    block = hook._format_block([hit])
    line = next(ln for ln in block.split("\n") if ln.startswith("D061"))
    assert 'supersedes D060 "use dynamodb"' in line
    assert 'rejected "use dynamodb" in favor of "adopt postgres"' in line
    marker = 'in favor of "adopt postgres".'
    snippet = line[line.index(marker) + len(marker) :].strip()
    assert snippet.endswith("…")
    assert len(snippet) <= hook.PREVIEW_CHARS


# ── guaranteed slot for the top superseding decision: _select_injected ─────────


def _cand(number: int, *, ref: bool = False) -> dict:
    """Build a minimal candidate dict for _select_injected unit coverage."""
    hit: dict = {"number": number}
    if ref:
        hit["supersedes_ref"] = {"number": number - 1, "title": f"old {number - 1}"}
    return hit


def test_select_injected_ref_in_top3_unchanged():
    """A superseding hit already inside the cap leaves the top-3 untouched."""
    candidates = [_cand(1, ref=True), _cand(2), _cand(3), _cand(4, ref=True)]
    assert hook._select_injected(candidates) == candidates[:3]


def test_select_injected_no_ref_anywhere_unchanged():
    """With no superseding hit at all, the plain top-3 is returned."""
    candidates = [_cand(1), _cand(2), _cand(3), _cand(4), _cand(5)]
    assert hook._select_injected(candidates) == candidates[:3]


def test_select_injected_swaps_ref_from_index_three():
    """A superseding hit below the cap takes the last slot, keeping ranks 1-2."""
    candidates = [_cand(1), _cand(2), _cand(3), _cand(4, ref=True)]
    result = hook._select_injected(candidates)
    assert result == [candidates[0], candidates[1], candidates[3]]


def test_select_injected_only_first_ref_below_cap_swaps():
    """Only the first superseding hit below the cap is promoted; later ones are not."""
    candidates = [_cand(1), _cand(2), _cand(3), _cand(4, ref=True), _cand(5, ref=True)]
    result = hook._select_injected(candidates)
    assert result == [candidates[0], candidates[1], candidates[3]]
    assert candidates[4] not in result


def test_select_injected_fewer_than_cap_no_swap():
    """Fewer candidates than the cap returns them all, with no swap and no crash."""
    candidates = [_cand(1), _cand(2)]
    assert hook._select_injected(candidates) == candidates


# ── guaranteed slot: integration through the invocation path ───────────────────


def _fixed_hit(number: int, title: str) -> dict:
    """Build a full kernel-shaped hit dict that clears any corpus-scaled floor."""
    return {
        "number": number,
        "title": title,
        "score": 20.0,
        "status": "active",
        "date": "2026-05-01",
        "preview": f"rationale preview for {title}",
    }


def _patch_fixed_check(monkeypatch, hits: list[dict]) -> None:
    """Patch hook._check to return a fixed ordered hit list, bypassing the kernel."""
    monkeypatch.setattr(hook, "_check", lambda store_path, prompt: [dict(h) for h in hits])


def _seed_reversal_at_index_three(tmp_path: Path, monkeypatch) -> Path:
    """Seed a real reversal pair and a 5-hit order with the reversal at index 3.

    Ranks 1-3 are non-superseding distractors; the superseding decision (D201,
    which supersedes D200) sits at index 3, below the cap. Returns the repo dir.
    """
    repo, store_path = _make_project(tmp_path)
    _write_decision(
        store_path,
        num=200,
        title="use dynamodb as the primary datastore",
        rationale="dynamodb chosen for the primary datastore key-value access",
        status="superseded",
        superseded_by="201",
    )
    _write_decision(
        store_path,
        num=201,
        title="adopt postgres as the primary datastore",
        rationale="postgres chosen over dynamodb for relational integrity and SQL tooling",
        supersedes="200",
    )
    hits = [
        _fixed_hit(100, "first unrelated choice"),
        _fixed_hit(101, "second unrelated choice"),
        _fixed_hit(102, "third unrelated choice"),
        _fixed_hit(201, "adopt postgres as the primary datastore"),
        _fixed_hit(103, "fifth unrelated choice"),
    ]
    _patch_fixed_check(monkeypatch, hits)
    return repo


def test_guaranteed_slot_swaps_in_reversal(tmp_path: Path, monkeypatch):
    """A reversal below the cap is promoted into the block with explicit wording."""
    repo = _seed_reversal_at_index_three(tmp_path, monkeypatch)
    result = _invoke(
        {"prompt": "adopt a primary datastore", "cwd": str(repo), "session_id": "swap-a"}
    )
    assert result.exit_code == 0
    assert result.output != ""
    ctx = json.loads(result.output)["hookSpecificOutput"]["additionalContext"]
    # The reversal is in the block, stated explicitly.
    assert "D201" in ctx
    assert 'supersedes D200 "use dynamodb as the primary datastore"' in ctx
    assert 'in favor of "adopt postgres as the primary datastore"' in ctx
    # Ranks 1-2 are retained; the weakest top hit is displaced.
    assert "D100" in ctx
    assert "D101" in ctx
    assert "D102" not in ctx


def test_guaranteed_slot_no_swap_on_unparseable_target(tmp_path: Path, monkeypatch):
    """A reversal whose target does not parse yields no ref, so no swap happens."""
    repo, store_path = _make_project(tmp_path)
    # A file that carries the 063 number prefix but does not parse as v2.
    (store_path / "decisions" / "063-broken.md").write_text("not a valid decision file at all")
    _write_decision(
        store_path,
        num=64,
        title="adopt kafka for the event stream bus",
        rationale="kafka chosen for the event stream bus",
        supersedes="63",
    )
    hits = [
        _fixed_hit(100, "first unrelated choice"),
        _fixed_hit(101, "second unrelated choice"),
        _fixed_hit(102, "third unrelated choice"),
        _fixed_hit(64, "adopt kafka for the event stream bus"),
        _fixed_hit(103, "fifth unrelated choice"),
    ]
    _patch_fixed_check(monkeypatch, hits)
    result = _invoke({"prompt": "adopt an event bus", "cwd": str(repo), "session_id": "swap-d"})
    assert result.exit_code == 0
    assert result.output != ""
    ctx = json.loads(result.output)["hookSpecificOutput"]["additionalContext"]
    # No ref resolves, so the plain top-3 renders and nothing is promoted.
    assert "D100" in ctx
    assert "D101" in ctx
    assert "D102" in ctx
    assert "D064" not in ctx
    assert "in favor of" not in ctx


def test_guaranteed_slot_records_swapped_in_number(tmp_path: Path, monkeypatch):
    """Session-seen state records the promoted decision, not the displaced one."""
    repo = _seed_reversal_at_index_three(tmp_path, monkeypatch)
    session_id = "swap-e"
    result = _invoke(
        {"prompt": "adopt a primary datastore", "cwd": str(repo), "session_id": session_id}
    )
    assert result.exit_code == 0
    state_file = tmp_path / "hook-state" / f"{session_id}.json"
    seen = json.loads(state_file.read_text())["seen"]
    assert 201 in seen
    assert 102 not in seen
