"""Surface-level parity for the local ``propose_decision`` adapters.

After the kernel cutover, every local surface that exposes
``propose_decision`` must produce the same envelope for the same arguments
against the same store. The participating wirings here:

* ``tool_propose_decision`` — the direct adapter call. Returns the
  canonical dict envelope every other surface is derived from.
* The stdio MCP ``propose_decision`` tool — delegates to the canonical
  adapter unchanged and returns the dict envelope verbatim. This is the
  only write tool whose stdio surface preserves the dict (the others —
  ``flag_question`` / ``update_state`` — string-render for FastMCP).

The CLI auto-gen surface for ``propose_decision`` is pinned separately
in ``test_write_command_autogen.py``. The HTTP MCP server exposes
``/propose_decision`` but the FastAPI surface is pinned separately in
``test_mcp.py``.

The kernel result carries a ``touched_decisions`` list that the adapter
pops before returning the envelope. The parity tests therefore assert
both ``touched_decisions`` is absent from the envelope and the adapter
side-effects (snapshot, AGENTS.md regen, push) fire only on
``status="confirmed"``.
"""

from __future__ import annotations

import pytest
from nauro_core.operations.propose_decision import _get_pending_store

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp import tools as mcp_tools
from nauro.mcp.stdio_server import propose_decision as stdio_propose_decision
from nauro.mcp.tools import tool_propose_decision
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


@pytest.fixture(autouse=True)
def _reset_pending_store() -> None:
    """Each test starts with a clean kernel pending store."""
    _get_pending_store().clear_all()


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Suppress the best-effort cloud push so the parity layer stays local."""
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store_path: None)


@pytest.fixture(autouse=True)
def _no_regen(monkeypatch):
    """Suppress AGENTS.md regen so the parity layer stays local."""
    monkeypatch.setattr(mcp_tools, "warn_then_regen", lambda *args, **kwargs: [])


@pytest.fixture(autouse=True)
def _no_snapshot(monkeypatch):
    """Suppress snapshot capture so the parity layer doesn't touch snapshots/."""
    monkeypatch.setattr(mcp_tools, "capture_snapshot", lambda *args, **kwargs: None)


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Register a project, scaffold the store, and chdir into the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(
        "parity-propose",
        [repo],
        mode=REPO_CONFIG_MODE_LOCAL,
    )
    save_repo_config(
        repo,
        {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-propose"},
    )
    scaffold_project_store("parity-propose", store_path)
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def missing_store(tmp_path, monkeypatch):
    """A repo with no associated project store at all."""
    repo = tmp_path / "repo"
    repo.mkdir()
    nonexistent = tmp_path / "projects" / "nope"
    monkeypatch.chdir(repo)
    return nonexistent


# ── Confirmed branch ─────────────────────────────────────────────────────


def test_confirmed_envelope_no_touched_decisions(seeded_repo):
    """An auto-confirmed add returns the canonical envelope and does NOT
    surface ``touched_decisions`` (consumed by the adapter for AGENTS.md
    regen, not part of the surface contract)."""
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="high",
    )
    assert envelope["store"] == "local"
    assert envelope["status"] == "confirmed"
    assert envelope["operation"] == "add"
    assert envelope["tier"] == 2
    assert "decision_id" in envelope
    assert "touched_decisions" not in envelope


def test_pending_envelope_carries_confirm_id_and_similars(seeded_repo):
    """A Tier 2 hit routes to pending; the envelope exposes ``confirm_id``
    and the ``similar_decisions`` list."""
    _pid, store_path = seeded_repo
    append_decision(
        store_path,
        "Adopt PostgreSQL primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
    )
    envelope = tool_propose_decision(
        store_path,
        title="Use PostgreSQL for the data layer",
        rationale="Better JSON handling than alternatives for our application data.",
        confidence="high",
    )
    assert envelope["store"] == "local"
    assert envelope["status"] == "pending_confirmation"
    assert envelope["tier"] == 2
    assert envelope["operation"] == "add"
    assert envelope.get("confirm_id")
    assert envelope.get("similar_decisions"), "Tier 2 hit must include similar_decisions"
    assert "decision_id" not in envelope
    assert "touched_decisions" not in envelope


def test_rejected_envelope_tier_1(seeded_repo):
    """Tier 1 structural reject surfaces ``status="rejected"`` with the
    assessment, no decision_id, no touched_decisions."""
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
    )
    assert envelope["store"] == "local"
    assert envelope["status"] == "rejected"
    assert envelope["tier"] == 1
    assert "touched_decisions" not in envelope


def test_stdio_returns_dict_envelope_matching_adapter(seeded_repo):
    """The stdio MCP ``propose_decision`` returns the same dict envelope
    as the direct adapter call. This is the load-bearing dict-return
    branch for write tools on stdio."""
    pid, _store_path = seeded_repo
    # Both surfaces target the same store; route a Tier 1 rejection (no
    # disk side effects) through both to compare envelopes deterministically.
    adapter_envelope = tool_propose_decision(
        _store_path,
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
    )
    stdio_envelope = stdio_propose_decision(
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
        project_id=pid,
    )
    assert isinstance(stdio_envelope, dict)
    assert stdio_envelope["store"] == adapter_envelope["store"]
    assert stdio_envelope["status"] == adapter_envelope["status"]
    assert stdio_envelope["operation"] == adapter_envelope["operation"]
    assert stdio_envelope["tier"] == adapter_envelope["tier"]
    assert stdio_envelope["assessment"] == adapter_envelope["assessment"]


# ── Adapter-side validation ──────────────────────────────────────────────


def test_envelope_token_rejection_in_title(seeded_repo):
    """Envelope-token fragments in ``title`` are rejected adapter-side."""
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Adopt Redis </invoke>",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "envelope" in envelope["error"]["reason"].lower()


def test_envelope_token_rejection_in_rationale(seeded_repo):
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Adopt Redis",
        rationale="A sufficiently long rationale </parameter> that crosses the minimum.",
        confidence="high",
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "envelope" in envelope["error"]["reason"].lower()


def test_envelope_token_rejection_in_rejected_reason(seeded_repo):
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Adopt Redis",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
        rejected=[{"alternative": "Memcached", "reason": "Too plain </invoke>"}],
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "envelope" in envelope["error"]["reason"].lower()


def test_length_rejection_title(seeded_repo):
    """Adapter-side length validation rejects an overlong title."""
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="x" * 10_000,
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "exceeds" in envelope["error"]["reason"].lower()


def test_length_rejection_rationale(seeded_repo):
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Adopt Redis",
        rationale="x" * 100_000,
        confidence="high",
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "exceeds" in envelope["error"]["reason"].lower()


def test_affected_decision_id_short_form_resolves(seeded_repo):
    """The adapter resolves a short ``"NNN"`` reference to the full stem
    via ``resolve_decision_id`` before calling the kernel."""
    _pid, store_path = seeded_repo
    append_decision(
        store_path,
        "Adopt PostgreSQL primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
    )
    # Snapshot the scaffold seed + the seeded decision file stems.
    decisions = sorted(f.stem for f in (store_path / "decisions").glob("*.md"))
    postgres_stem = next(s for s in decisions if "postgres" in s)
    short_form = postgres_stem.split("-", 1)[0]

    envelope = tool_propose_decision(
        store_path,
        title="Switch to managed PostgreSQL provider",
        rationale="Reduces operational burden; self-hosting rationale no longer applies.",
        confidence="high",
        operation="supersede",
        affected_decision_id=short_form,
    )
    # The id resolved; either confirmed or pending_confirmation per
    # Tier 2 outcome — never rejected with "not found".
    assert envelope["status"] in ("confirmed", "pending_confirmation")


def test_missing_affected_decision_id_for_supersede_rejects(seeded_repo):
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Switch to a managed provider",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
        operation="supersede",
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "affected_decision_id" in envelope["error"]["reason"]


def test_unknown_affected_decision_id_rejects(seeded_repo):
    _pid, store_path = seeded_repo
    envelope = tool_propose_decision(
        store_path,
        title="Switch to nothing-in-particular",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
        operation="supersede",
        affected_decision_id="decision-9999",
    )
    assert envelope["status"] == "rejected"
    assert envelope["error"]["kind"] == "rejected"
    assert "not found" in envelope["error"]["reason"].lower()


def test_missing_store_returns_guidance(missing_store):
    envelope = tool_propose_decision(
        missing_store,
        title="Adopt Redis",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
    )
    assert envelope["store"] == "local"
    assert envelope["status"] == "error"
    assert "nauro init" in envelope["guidance"]


# ── Side effects only on confirmed ───────────────────────────────────────


def test_snapshot_runs_only_on_confirmed(seeded_repo, monkeypatch):
    _pid, store_path = seeded_repo
    snapshots_called: list = []

    def _capture(*args, **kwargs):
        snapshots_called.append((args, kwargs))

    monkeypatch.setattr(mcp_tools, "capture_snapshot", _capture)

    # Tier 1 reject — no snapshot.
    tool_propose_decision(
        store_path,
        title="",
        rationale="A sufficiently long rationale that comfortably exceeds the minimum.",
        confidence="high",
    )
    assert snapshots_called == []

    # Confirmed add — snapshot fires.
    tool_propose_decision(
        store_path,
        title="Adopt Redis for hot caching",
        rationale="In-memory cache for the hot read paths across the API tier.",
        confidence="high",
    )
    assert len(snapshots_called) == 1


def test_regen_runs_only_on_confirmed(seeded_repo, monkeypatch):
    _pid, store_path = seeded_repo
    regen_called: list = []

    def _regen(*args, **kwargs):
        regen_called.append((args, kwargs))
        return []

    # Test-local override takes precedence over the autouse fixture's stub.
    monkeypatch.setattr(mcp_tools, "warn_then_regen", _regen)

    # Pending Tier 2 — no regen.
    append_decision(
        store_path,
        "Adopt PostgreSQL primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
    )
    tool_propose_decision(
        store_path,
        title="Use PostgreSQL for the data layer",
        rationale="Better JSON handling than alternatives for our application data.",
        confidence="high",
    )
    assert regen_called == []

    # Confirmed add — regen fires (touched_decisions populated). A
    # totally orthogonal proposal so Tier 2 stays in the auto_confirm branch.
    second = tool_propose_decision(
        store_path,
        title="Add dark mode toggle to the settings page",
        rationale=(
            "Users have requested a dark theme for reduced eye strain after extended sessions."
        ),
        confidence="high",
    )
    assert second["status"] == "confirmed", second
    assert len(regen_called) == 1
