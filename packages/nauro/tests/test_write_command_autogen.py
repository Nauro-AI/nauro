"""End-to-end tests for the auto-generated ``propose-decision`` and
``confirm-decision`` CLI commands.

Three concerns are pinned here:

* Happy paths through the write tools land the same envelope the adapter
  returns (auto-confirmed adds, supersedes with ``--operation supersede
  --affected-decision-id``, pending-confirmation followed by
  ``confirm-decision``).
* Rejection paths surface kernel ``status="rejected"`` envelopes on
  stdout at exit 0 (caller-fixable) and ``status="error"`` guidance on
  stderr at exit 1 (transport-level).
* CLI argument-parse failures (``--rejected`` malformed / wrong shape)
  raise ``typer.BadParameter`` and exit 2 with stderr text, never
  invoking the adapter. The ``list[str]`` (``--files-affected``)
  repeated-flag form and ``list[dict]`` (``--rejected``) literal /
  ``@file`` / ``-`` (stdin) sigil forms are exercised here so the
  framework convention is pinned at the CLI surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nauro_core.constants import MAX_RATIONALE_LENGTH
from nauro_core.operations.propose_decision import _get_pending_store
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_LOCAL
from nauro.mcp import tools as mcp_tools
from nauro.store.registry import register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.templates.scaffolds import scaffold_project_store
from tests._ansi import strip_ansi
from tests._writer_compat import append_decision

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_pending_store() -> None:
    """Each test starts with a clean kernel pending store."""
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Suppress the best-effort cloud push so the CLI layer stays local."""
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store_path: None)


@pytest.fixture(autouse=True)
def _no_regen(monkeypatch):
    """Suppress AGENTS.md regen so the CLI layer stays local."""
    monkeypatch.setattr(mcp_tools, "warn_then_regen", lambda *args, **kwargs: [])


@pytest.fixture(autouse=True)
def _no_snapshot(monkeypatch):
    """Suppress snapshot capture so the CLI layer doesn't touch snapshots/."""
    monkeypatch.setattr(mcp_tools, "capture_snapshot", lambda *args, **kwargs: None)


@pytest.fixture
def seeded_repo(tmp_path: Path, monkeypatch) -> tuple[str, Path, Path]:
    """Register a project, scaffold the store, and chdir into the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pid, store_path = register_project_v2(
        "cli-write",
        [repo],
        mode=REPO_CONFIG_MODE_LOCAL,
    )
    save_repo_config(
        repo,
        {"mode": REPO_CONFIG_MODE_LOCAL, "id": pid, "name": "cli-write"},
    )
    scaffold_project_store("cli-write", store_path)
    monkeypatch.chdir(repo)
    return pid, store_path, repo


# ── Happy paths ─────────────────────────────────────────────────────────────


class TestProposeDecisionHappyPaths:
    def test_clean_add_lands_confirmed(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert envelope["status"] == "confirmed"
        assert envelope["operation"] == "add"
        assert "decision_id" in envelope

    def test_skip_validation_flag(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--skip-validation",
            ],
        )
        # skip_validation bypasses Tier 2, so the auto-confirm short-circuit
        # never runs; Tier 1 hands off via confirm_id.
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "pending_confirmation"
        assert envelope["tier"] == 1
        assert envelope.get("confirm_id")

    def test_supersede_with_affected_decision_id(self, seeded_repo) -> None:
        _pid, store_path, _repo = seeded_repo
        append_decision(
            store_path,
            "Adopt PostgreSQL primary database",
            rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        )
        decisions = sorted(f.stem for f in (store_path / "decisions").glob("*.md"))
        postgres_stem = next(s for s in decisions if "postgres" in s)
        short_form = postgres_stem.split("-", 1)[0]

        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Switch to managed PostgreSQL provider",
                "Reduces operational burden; self-hosting rationale no longer applies.",
                "--operation",
                "supersede",
                "--affected-decision-id",
                short_form,
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        # Either confirmed (Tier 2 clean) or pending_confirmation (Tier 2 hit);
        # both are acceptable — the point is the affected_decision_id resolved.
        assert envelope["status"] in ("confirmed", "pending_confirmation")
        assert envelope["operation"] == "supersede"


class TestConfirmDecisionHappyPath:
    def test_confirm_after_pending(self, seeded_repo) -> None:
        _pid, store_path, _repo = seeded_repo
        # Seed a similar decision so Tier 2 routes the next add to pending.
        append_decision(
            store_path,
            "Adopt PostgreSQL primary database",
            rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        )
        propose = runner.invoke(
            app,
            [
                "propose-decision",
                "Use PostgreSQL for the data layer",
                "Better JSON handling than alternatives for our application data.",
            ],
        )
        assert propose.exit_code == 0, propose.output
        propose_env = json.loads(propose.stdout)
        assert propose_env["status"] == "pending_confirmation"
        confirm_id = propose_env["confirm_id"]

        confirm = runner.invoke(app, ["confirm-decision", confirm_id])
        assert confirm.exit_code == 0, confirm.output
        confirm_env = json.loads(confirm.stdout)
        assert confirm_env["store"] == "local"
        assert confirm_env["status"] == "confirmed"
        assert "decision_id" in confirm_env


# ── Rejection paths ─────────────────────────────────────────────────────────


class TestProposeDecisionRejections:
    def test_update_without_affected_id_rejected(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Tweak PostgreSQL rationale",
                "Add operational notes after the first month of production use.",
                "--operation",
                "update",
            ],
        )
        # Caller-fixable rejection: envelope on stdout, exit 0.
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "rejected"
        assert envelope["error"]["kind"] == "rejected"
        assert "affected_decision_id" in envelope["error"]["reason"]

    def test_update_with_unknown_affected_id_rejected(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Tweak something that does not exist",
                "Add operational notes after the first month of production use.",
                "--operation",
                "update",
                "--affected-decision-id",
                "9999",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "rejected"
        assert envelope["error"]["kind"] == "rejected"
        assert "not found" in envelope["error"]["reason"].lower()

    def test_overlong_rationale_rejected(self, seeded_repo) -> None:
        oversized = "x" * (MAX_RATIONALE_LENGTH + 1)
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                oversized,
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "rejected"
        assert envelope["error"]["kind"] == "rejected"
        assert "exceeds" in envelope["error"]["reason"].lower()

    def test_propose_missing_store_exits_one_with_guidance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # No project registered, no repo config — the resolver fails before
        # the adapter is reached.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
            ],
        )
        assert result.exit_code == 1
        assert "No project found" in result.output


class TestConfirmDecisionRejections:
    def test_unknown_confirm_id_rejection_envelope(self, seeded_repo) -> None:
        result = runner.invoke(app, ["confirm-decision", "no-such-id"])
        # Unknown confirm_ids land status="rejected" with a structured
        # ErrorPayload; the wrapper exits 0 — the envelope on stdout
        # carries the reason and the user can fix the call.
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "rejected"
        assert envelope["error"]["kind"] == "rejected"
        assert "confirm_id" in envelope["error"]["reason"].lower()


# ── Flag-shape coverage ─────────────────────────────────────────────────────


class TestFlagShapes:
    def test_files_affected_repeats(self, seeded_repo) -> None:
        _pid, store_path, _repo = seeded_repo
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--files-affected",
                "a.py",
                "--files-affected",
                "b.py",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "confirmed"
        # The repeated flag landed as ["a.py", "b.py"] in the written
        # decision file's frontmatter.
        decision_file = next((store_path / "decisions").glob("*adopt-redis*.md"))
        body = decision_file.read_text()
        assert "- a.py" in body
        assert "- b.py" in body

    def test_rejected_literal_json(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--rejected",
                '[{"alternative": "Memcached", "reason": "Less feature-rich"}]',
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "confirmed"

    def test_rejected_at_file_sigil(self, seeded_repo, tmp_path: Path) -> None:
        payload = tmp_path / "rejected.json"
        payload.write_text('[{"alternative": "Memcached", "reason": "Less feature-rich"}]')
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--rejected",
                f"@{payload}",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "confirmed"

    def test_rejected_stdin_sigil(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--rejected",
                "-",
            ],
            input='[{"alternative": "Memcached", "reason": "Less feature-rich"}]',
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "confirmed"

    def test_rejected_malformed_json_exits_two(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--rejected",
                "not json",
            ],
        )
        # typer.BadParameter renders to stderr at exit 2.
        assert result.exit_code == 2
        assert "--rejected" in strip_ansi(result.output)
        assert "invalid JSON" in result.output

    def test_rejected_wrong_shape_exits_two(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Adopt Redis for hot caching",
                "In-memory cache for the hot read paths across the API tier.",
                "--rejected",
                '{"not": "a list"}',
            ],
        )
        assert result.exit_code == 2
        assert "--rejected" in strip_ansi(result.output)
        assert "expected JSON array of objects" in result.output
