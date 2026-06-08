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
) -> None:
    """Write a valid v2 decision file into the store's decisions directory."""
    decision = Decision(
        date=dt.date(2026, 5, 1),
        confidence="medium",
        status=status,
        num=num,
        title=title,
        rationale=rationale,
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
