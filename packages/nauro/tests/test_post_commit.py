"""The post-commit seam runs every ancillary step and never raises.

Covers the runner in isolation (a clean run, a degraded snapshot capture, a
degraded AGENTS.md regeneration raising something other than ``OSError``, a
raising push callable, and the ordering guarantee that a degraded step does not
skip the ones after it), then the MCP write adapters that route through it: a
degraded ancillary step must leave the success envelope and the journal event
intact. The CLI surfaces are covered alongside their own commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nauro.mcp import tools as mcp_tools
from nauro.mcp.tools import tool_flag_question, tool_propose_decision, tool_update_state
from nauro.store.journal import read_events
from nauro.store.post_commit import AncillaryStep, run_post_commit
from nauro.store.registry import register_project_v2
from nauro.sync.push import PushReport
from nauro.templates.scaffolds import scaffold_project_store
from tests.conftest import active_decision_markdown

SNAPSHOTS = "snapshots"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """Register a project whose single repo path is a real directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store_path = register_project_v2("postcommitproj", [repo])
    scaffold_project_store("postcommitproj", store_path)
    return store_path, repo


def test_clean_run_reports_no_degradation_and_the_regenerated_repos(tmp_path: Path) -> None:
    store_path, repo = _project(tmp_path)

    outcome = run_post_commit(
        store_path,
        snapshot_trigger="decision: 001-x",
        regenerate_agents_md=True,
    )

    assert outcome.is_degraded is False
    assert outcome.degraded == ()
    assert outcome.warnings == ()
    assert outcome.updated_repos == (repo,)
    assert list((store_path / SNAPSHOTS).glob("v*.json"))
    assert (repo / "AGENTS.md").exists()


def test_snapshot_oserror_degrades_the_step_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path, repo = _project(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr("nauro.store.post_commit.capture_snapshot", _boom)

    outcome = run_post_commit(
        store_path,
        snapshot_trigger="decision: 001-x",
        regenerate_agents_md=True,
    )

    assert [d.step for d in outcome.degraded] == [AncillaryStep.SNAPSHOT]
    assert "disk full" in outcome.degraded[0].reason
    assert any("snapshot capture" in line for line in outcome.warnings)
    # The next step still ran.
    assert outcome.updated_repos == (repo,)
    assert (repo / "AGENTS.md").exists()


def test_non_oserror_from_regen_degrades_the_step_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path, _repo = _project(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> list[Path]:
        raise ValueError("registry entry is malformed")

    monkeypatch.setattr("nauro.store.post_commit.warn_then_regen", _boom)

    outcome = run_post_commit(
        store_path,
        snapshot_trigger="decision: 001-x",
        regenerate_agents_md=True,
    )

    assert [d.step for d in outcome.degraded] == [AncillaryStep.AGENTS_MD]
    assert "registry entry is malformed" in outcome.degraded[0].reason
    assert outcome.updated_repos == ()
    # The earlier step still committed its artifact.
    assert list((store_path / SNAPSHOTS).glob("v*.json"))


def test_every_step_runs_when_the_earlier_ones_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path, _repo = _project(tmp_path)
    pushed: list[Path] = []

    def _snapshot_boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("disk full")

    def _regen_boom(*_args: object, **_kwargs: object) -> list[Path]:
        raise ValueError("registry entry is malformed")

    monkeypatch.setattr("nauro.store.post_commit.capture_snapshot", _snapshot_boom)
    monkeypatch.setattr("nauro.store.post_commit.warn_then_regen", _regen_boom)

    outcome = run_post_commit(
        store_path,
        snapshot_trigger="decision: 001-x",
        regenerate_agents_md=True,
        push=pushed.append,
    )

    assert [d.step for d in outcome.degraded] == [AncillaryStep.SNAPSHOT, AncillaryStep.AGENTS_MD]
    assert len(outcome.warnings) == 2
    assert pushed == [store_path]


def test_a_raising_push_degrades_the_step_without_raising(tmp_path: Path) -> None:
    """The seam guards whatever a caller injects, not just its own two steps."""
    store_path, repo = _project(tmp_path)

    def _boom(_store_path: Path) -> None:
        raise RuntimeError("S3 credentials expired")

    outcome = run_post_commit(
        store_path,
        snapshot_trigger="decision: 001-x",
        regenerate_agents_md=True,
        push=_boom,
    )

    assert [d.step for d in outcome.degraded] == [AncillaryStep.PUSH]
    assert "S3 credentials expired" in outcome.degraded[0].reason
    assert any("cloud push" in line for line in outcome.warnings)
    # The steps before it kept their artifacts.
    assert list((store_path / SNAPSHOTS).glob("v*.json"))
    assert outcome.updated_repos == (repo,)


def test_warn_channel_is_threaded_into_the_regeneration(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _pid, store_path = register_project_v2("postcommitwarn", [repo])
    scaffold_project_store("postcommitwarn", store_path)
    warnings: list[str] = []

    outcome = run_post_commit(store_path, regenerate_agents_md=True, warn=warnings.append)

    assert outcome.is_degraded is False
    assert any("repo path does not exist" in line for line in warnings)


# ── MCP write adapters ───────────────────────────────────────────────────


RATIONALE = "In-memory cache for the hot read paths across the API tier and pub/sub channels."


@pytest.fixture()
def adapter_store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "adapterproj"
    scaffold_project_store("adapterproj", store_path)
    return store_path


@pytest.fixture()
def no_push(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the cloud push instead of attempting it."""
    pushed: list[Path] = []
    monkeypatch.setattr(mcp_tools, "_try_push", pushed.append)
    return pushed


def _raise_oserror(*_args: object, **_kwargs: object) -> int:
    raise OSError("disk full")


def test_degraded_snapshot_keeps_the_propose_envelope_and_the_journal_event(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch, no_push: list[Path]
) -> None:
    monkeypatch.setattr("nauro.store.post_commit.capture_snapshot", _raise_oserror)

    envelope = tool_propose_decision(
        adapter_store,
        title="Use Redis for hot caching",
        rationale=RATIONALE,
        confidence="medium",
    )

    assert envelope["status"] == "confirmed"
    assert envelope["decision_id"]
    # The journal decorator emits after the adapter returns, so an ancillary
    # exception used to destroy the event for a decision that had committed.
    events = read_events(adapter_store)
    assert [e.status for e in events] == ["committed"]
    assert events[0].decision_id == envelope["decision_id"]
    assert no_push == [adapter_store]


def test_a_raising_push_keeps_the_propose_envelope_and_the_journal_event(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_store_path: Path) -> None:
        raise RuntimeError("S3 credentials expired")

    monkeypatch.setattr(mcp_tools, "_try_push", _boom)

    envelope = tool_propose_decision(
        adapter_store,
        title="Use Redis for hot caching",
        rationale=RATIONALE,
        confidence="medium",
    )

    assert envelope["status"] == "confirmed"
    assert "cloud push failed" in envelope["assessment"]
    assert [e.status for e in read_events(adapter_store)] == ["committed"]


def test_degraded_regen_notes_the_step_in_the_assessment_and_still_pushes(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch, no_push: list[Path]
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> list[Path]:
        raise ValueError("registry entry is malformed")

    monkeypatch.setattr("nauro.store.post_commit.warn_then_regen", _boom)

    envelope = tool_propose_decision(
        adapter_store,
        title="Use Redis for hot caching",
        rationale=RATIONALE,
        confidence="medium",
    )

    assert envelope["status"] == "confirmed"
    assert "AGENTS.md regeneration failed" in envelope["assessment"]
    assert "registry entry is malformed" in envelope["assessment"]
    assert set(envelope) <= {
        "store",
        "project",
        "status",
        "tier",
        "operation",
        "assessment",
        "decision_id",
        "similar_decisions",
        "resolved_questions",
        "relocated_ids",
        "skipped_prose_ids",
        "error",
    }
    assert no_push == [adapter_store]


def test_degraded_snapshot_keeps_the_flag_question_envelope(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch, no_push: list[Path]
) -> None:
    monkeypatch.setattr("nauro.store.post_commit.capture_snapshot", _raise_oserror)

    envelope = tool_flag_question(adapter_store, question="Should the cache be write-through?")

    assert envelope["status"] == "ok"
    assert "snapshot capture failed" in envelope["assessment"]
    assert "disk full" in envelope["assessment"]
    assert [e.status for e in read_events(adapter_store)] == ["committed"]
    assert no_push == [adapter_store]


def test_degraded_push_reaches_the_flag_question_resolve_envelope(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (adapter_store / "open-questions.md").write_text(
        "# Open Questions\n\n- [Q1] Should the cache be write-through?\n"
    )
    decisions = adapter_store / "decisions"
    decisions.mkdir(exist_ok=True)
    (decisions / "042-caching.md").write_text(active_decision_markdown(42, "Caching"))

    def _boom(_store_path: Path) -> None:
        raise RuntimeError("S3 credentials expired")

    monkeypatch.setattr(mcp_tools, "_try_push", _boom)

    envelope = tool_flag_question(adapter_store, resolved_by="D42", targets=["Q1"])

    assert envelope["status"] == "ok"
    assert "cloud push failed" in envelope["assessment"]
    assert "S3 credentials expired" in envelope["assessment"]


def test_degraded_snapshot_keeps_the_update_state_envelope(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch, no_push: list[Path]
) -> None:
    monkeypatch.setattr("nauro.store.post_commit.capture_snapshot", _raise_oserror)

    envelope = tool_update_state(adapter_store, delta="Shipped the caching layer.")

    assert envelope["status"] == "ok"
    assert "snapshot capture failed" in envelope["assessment"]
    assert "disk full" in envelope["assessment"]
    # The kernel's own advisory field is untouched: adapter text never lands in it.
    assert "snapshot" not in envelope.get("warning", "")
    assert [e.status for e in read_events(adapter_store)] == ["committed"]
    assert no_push == [adapter_store]


def test_a_clean_run_adds_no_assessment_to_the_short_envelopes(
    adapter_store: Path, no_push: list[Path]
) -> None:
    """The degraded channel is silent when nothing degraded.

    ``assessment`` is a field these two envelopes do not otherwise carry, so it
    may only appear when there is something to say.
    """
    flagged = tool_flag_question(adapter_store, question="Should the cache be write-through?")
    updated = tool_update_state(adapter_store, delta="Shipped the caching layer.")

    assert "assessment" not in flagged
    assert "assessment" not in updated


def test_an_incomplete_nonraising_push_reaches_the_mcp_assessment(
    adapter_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = PushReport(
        planned=("stack.md",),
        ambiguous=("stack.md",),
    )
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store: report)

    envelope = tool_update_state(adapter_store, delta="Shipped the caching layer.")

    assert envelope["status"] == "ok"
    assert "cloud push failed" in envelope["assessment"]
    assert "pending observation" in envelope["assessment"]
    assert "stack.md" not in envelope["assessment"]
