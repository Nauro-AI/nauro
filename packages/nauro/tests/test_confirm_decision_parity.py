"""Surface-level parity for the local ``confirm_decision`` adapters.

After the kernel cutover, every local surface that exposes
``confirm_decision`` must produce the same envelope for the same
arguments against the same store. The participating wirings here:

* ``tool_confirm_decision`` — the direct adapter call. Returns the
  canonical dict envelope every other surface is derived from.
* The stdio MCP ``confirm_decision`` tool — delegates to the canonical
  adapter unchanged and returns the dict envelope verbatim (per the
  dict-return contract for write tools on stdio).

The kernel result carries a ``touched_decisions`` list that the adapter
pops before returning the envelope. The parity tests assert that the
list is absent from the wire envelope, the unknown-id rejection arrives
as a structured error payload, the half-state error surfaces in the same
shape, and the adapter side effects (AGENTS.md regen, push, snapshot)
fire only on the expected branches. Snapshot capture is asserted NOT to
fire on this path — the pre-cutover confirm surface did not capture, and
that asymmetry with propose is intentional.
"""

from __future__ import annotations

import pytest
from nauro_core.operations.propose_decision import _get_pending_store

from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp import tools as mcp_tools
from nauro.mcp.stdio_server import confirm_decision as stdio_confirm_decision
from nauro.mcp.tools import tool_confirm_decision
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


@pytest.fixture(autouse=True)
def _reset_pending_store() -> None:
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store_path: None)


@pytest.fixture(autouse=True)
def _no_regen(monkeypatch):
    monkeypatch.setattr(mcp_tools, "warn_then_regen", lambda *args, **kwargs: [])


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    """Register a project, scaffold the store, and chdir into the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(
        "parity-confirm",
        [repo],
        mode=REPO_CONFIG_MODE_LOCAL,
    )
    save_repo_config(
        repo,
        {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "parity-confirm"},
    )
    scaffold_project_store("parity-confirm", store_path)
    monkeypatch.chdir(repo)
    return pid, store_path


@pytest.fixture
def missing_store(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    nonexistent = tmp_path / "projects" / "nope"
    monkeypatch.chdir(repo)
    return nonexistent


def _seed_pending_add(title: str = "Adopt Redis for hot caching") -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": title,
                "rationale": (
                    "In-memory cache for hot read paths across the API tier and pub/sub channels."
                ),
                "confidence": "medium",
            },
            "operation": "add",
            "affected_decision_id": None,
        },
        {"tier": 1, "operation": "add", "similar_decisions": [], "assessment": "seed"},
    )


def _seed_pending_supersede(affected_decision_id: str) -> str:
    return _get_pending_store().store(
        {
            "proposal": {
                "title": "Switch to managed Postgres provider",
                "rationale": (
                    "Reduces operational burden; the self-hosting rationale no longer applies."
                ),
                "confidence": "medium",
            },
            "operation": "supersede",
            "affected_decision_id": affected_decision_id,
        },
        {"tier": 2, "operation": "supersede", "similar_decisions": [], "assessment": "seed"},
    )


# ── Confirmed branch ─────────────────────────────────────────────────────


def test_confirmed_envelope_no_touched_decisions(seeded_repo):
    """A confirmed add returns the canonical envelope and does NOT surface
    ``touched_decisions`` (consumed by the adapter for AGENTS.md regen)."""
    _pid, store_path = seeded_repo
    confirm_id = _seed_pending_add()
    envelope = tool_confirm_decision(store_path, confirm_id)
    assert envelope["store"] == "local"
    assert envelope["status"] == "confirmed"
    assert envelope["operation"] == "add"
    assert "decision_id" in envelope
    assert "touched_decisions" not in envelope


# ── Rejected: unknown id ─────────────────────────────────────────────────


def test_unknown_id_returns_structured_rejection(seeded_repo):
    _pid, store_path = seeded_repo
    envelope = tool_confirm_decision(store_path, "no-such-id")
    assert envelope == {
        "store": "local",
        "status": "rejected",
        "operation": "reject",
        "error": {"kind": "rejected", "reason": "Invalid or expired confirm_id."},
    }


# ── Rejected: half-state ─────────────────────────────────────────────────


def test_half_state_supersede_returns_structured_error(seeded_repo, monkeypatch):
    """A second-write failure during supersede surfaces a structured error
    envelope. The adapter still pops ``touched_decisions``."""
    _pid, store_path = seeded_repo
    append_decision(
        store_path,
        "Adopt PostgreSQL primary database",
        rationale="Mature ecosystem with strong JSON support and excellent tooling.",
    )
    # Identify the seeded stem to drive the supersede pending entry.
    decisions = sorted(f.stem for f in (store_path / "decisions").glob("*.md"))
    postgres_stem = next(s for s in decisions if "postgres" in s)
    confirm_id = _seed_pending_supersede(postgres_stem)

    # Wedge the old-decision flip. The kernel writes through Store.write_file;
    # patch FilesystemStore to fail only when the kernel writes back to the
    # seeded old-decision stem (the flip write).
    from nauro.store import filesystem_store

    original_write = filesystem_store.FilesystemStore.write_file
    failures_for = f"{postgres_stem}.md"

    def _failing_write(self, path: str, content: str) -> None:
        if path.endswith(failures_for):
            raise OSError("simulated old-decision flip failure")
        original_write(self, path, content)

    monkeypatch.setattr(filesystem_store.FilesystemStore, "write_file", _failing_write)

    envelope = tool_confirm_decision(store_path, confirm_id)
    assert envelope["store"] == "local"
    assert envelope["status"] == "rejected"
    assert envelope["operation"] == "supersede"
    assert envelope["error"]["kind"] == "error"
    assert "half-state" in envelope["error"]["reason"]
    assert "touched_decisions" not in envelope


# ── Missing store ───────────────────────────────────────────────────────


def test_missing_store_returns_guidance(missing_store):
    envelope = tool_confirm_decision(missing_store, "any-id")
    assert envelope["store"] == "local"
    assert envelope["status"] == "error"
    assert "nauro init" in envelope["guidance"]


# ── Side effects: push only on confirmed ────────────────────────────────


def test_push_only_called_on_confirmed(seeded_repo, monkeypatch):
    _pid, store_path = seeded_repo
    push_calls: list = []

    monkeypatch.setattr(mcp_tools, "_try_push", lambda store_path: push_calls.append(store_path))

    # Unknown id — no push.
    tool_confirm_decision(store_path, "no-such-id")
    assert push_calls == []

    # Confirmed add — push fires.
    confirm_id = _seed_pending_add()
    tool_confirm_decision(store_path, confirm_id)
    assert push_calls == [store_path]


# ── Side effects: regen only when touched non-empty AND confirmed ───────


def test_regen_runs_only_when_touched_and_confirmed(seeded_repo, monkeypatch):
    _pid, store_path = seeded_repo
    regen_calls: list = []

    monkeypatch.setattr(
        mcp_tools,
        "warn_then_regen",
        lambda *args, **kwargs: regen_calls.append((args, kwargs)) or [],
    )

    # Unknown id — no regen.
    tool_confirm_decision(store_path, "bad-id")
    assert regen_calls == []

    # Confirmed add — regen fires (touched_decisions populated).
    confirm_id = _seed_pending_add()
    envelope = tool_confirm_decision(store_path, confirm_id)
    assert envelope["status"] == "confirmed"
    assert len(regen_calls) == 1


# ── Snapshot capture is NOT called on confirm (byte-parity pin) ─────────


def test_snapshot_not_called_on_confirm(seeded_repo, monkeypatch):
    """The pre-cutover ``tool_confirm_decision`` did NOT capture a snapshot.
    Propose's auto-confirm path DOES. This asymmetry stays intentional;
    pin it with an explicit assertion."""
    _pid, store_path = seeded_repo
    snapshot_calls: list = []

    monkeypatch.setattr(
        mcp_tools,
        "capture_snapshot",
        lambda *args, **kwargs: snapshot_calls.append((args, kwargs)),
    )

    confirm_id = _seed_pending_add()
    envelope = tool_confirm_decision(store_path, confirm_id)
    assert envelope["status"] == "confirmed"
    assert snapshot_calls == []


# ── stdio adapter parity ────────────────────────────────────────────────


def test_stdio_returns_dict_envelope_matching_adapter(seeded_repo):
    """The stdio ``confirm_decision`` returns the same dict envelope as
    the direct adapter call. Route an unknown id through both so the
    comparison is deterministic and disk-side-effect-free."""
    pid, store_path = seeded_repo

    adapter_envelope = tool_confirm_decision(store_path, "no-such-id")
    stdio_envelope = stdio_confirm_decision(confirm_id="no-such-id", project_id=pid)

    assert isinstance(stdio_envelope, dict)
    assert stdio_envelope == adapter_envelope
