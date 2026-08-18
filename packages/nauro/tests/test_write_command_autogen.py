"""End-to-end tests for the auto-generated write CLI commands.

Three concerns are pinned here for ``propose-decision``:

* Happy paths through the write tool land the same envelope the adapter
  returns (auto-confirmed adds, supersedes with ``--operation supersede
  --affected-decision-id``).
* Rejection paths surface kernel ``status="rejected"`` envelopes on
  stdout at exit 0 (caller-fixable) and ``status="error"`` guidance on
  stderr at exit 1 (transport-level).
* CLI argument-parse failures (``--rejected`` malformed / wrong shape)
  raise ``typer.BadParameter`` and exit 2 with stderr text, never
  invoking the adapter. The ``list[str]`` (``--files-affected``)
  repeated-flag form and ``list[dict]`` (``--rejected``) literal /
  ``@file`` / ``-`` (stdin) sigil forms are exercised here so the
  framework convention is pinned at the CLI surface.

``flag-question`` pins the primary-positional convention: the optional
``question`` property accepts both the positional and ``--question``
spellings, supplying both is a usage error, and the ``--resolved-by``
resolve action still works with the positional omitted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nauro_core.constants import MAX_RATIONALE_LENGTH
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import tools as mcp_tools
from nauro.store import post_commit as post_commit_module
from tests._ansi import strip_ansi
from tests._writer_compat import append_decision
from tests.conftest import active_decision_markdown, register_v2_repo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Suppress the best-effort cloud push so the CLI layer stays local."""
    monkeypatch.setattr(mcp_tools, "_try_push", lambda _store_path: None)


@pytest.fixture(autouse=True)
def _no_regen(monkeypatch):
    """Suppress AGENTS.md regen so the CLI layer stays local."""
    monkeypatch.setattr(post_commit_module, "warn_then_regen", lambda *args, **kwargs: [])


@pytest.fixture(autouse=True)
def _no_snapshot(monkeypatch):
    """Suppress snapshot capture so the CLI layer doesn't touch snapshots/."""
    monkeypatch.setattr(post_commit_module, "capture_snapshot", lambda *args, **kwargs: None)


@pytest.fixture
def seeded_repo(tmp_path: Path, monkeypatch) -> tuple[str, Path, Path]:
    """Register a project, scaffold the store, and chdir into the repo."""
    result = register_v2_repo(tmp_path, "cli-write", monkeypatch=monkeypatch)
    return result.pid, result.store_path, result.repo


# ── Happy paths ─────────────────────────────────────────────────────────────


class TestProposeDecisionHappyPaths:
    def test_clean_add_lands_confirmed(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert envelope["status"] == "confirmed"
        assert envelope["operation"] == "add"
        assert "decision_id" in envelope

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
                "Reduces operational burden; self-hosting rationale no longer applies.",
                "--title",
                "Switch to managed PostgreSQL provider",
                "--operation",
                "supersede",
                "--affected-decision-id",
                short_form,
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        # The kernel commits on the same call; affected_decision_id resolved.
        assert envelope["status"] == "confirmed"
        assert envelope["operation"] == "supersede"

    def test_update_rationale_only_no_title_confirms_and_preserves_title(self, seeded_repo) -> None:
        """A rationale-only ``--operation update`` with no ``--title`` confirms
        end-to-end and leaves the target decision's title untouched.

        This is the path the old schema deadlocked: ``title`` was a required
        positional, so a schema-respecting CLI client could never omit it, yet
        the kernel rejects a non-empty title on update. With ``title`` optional
        the positional is gone and the update is callable. Title preservation is
        asserted on the rewritten decision file, not just the envelope.
        """
        _pid, store_path, _repo = seeded_repo
        original_title = "Adopt PostgreSQL primary database"
        append_decision(
            store_path,
            original_title,
            rationale="Mature ecosystem with strong JSON support and excellent tooling.",
        )
        decisions = sorted(f.stem for f in (store_path / "decisions").glob("*.md"))
        postgres_stem = next(s for s in decisions if "postgres" in s)
        short_form = postgres_stem.split("-", 1)[0]

        result = runner.invoke(
            app,
            [
                "propose-decision",
                "Add a managed-extensions clause after the first month in production.",
                "--operation",
                "update",
                "--affected-decision-id",
                short_form,
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "confirmed"
        assert envelope["operation"] == "update"

        # The target's title survives the rationale append; the rationale grows.
        body = (store_path / "decisions" / f"{postgres_stem}.md").read_text()
        assert f"— {original_title}" in body
        assert "managed-extensions clause" in body
        assert "version: 2" in body


# ── Rejection paths ─────────────────────────────────────────────────────────


class TestProposeDecisionRejections:
    def test_update_without_affected_id_rejected(self, seeded_repo) -> None:
        # A real rationale-only update omits --title; the adapter rejects the
        # missing affected_decision_id before the kernel is reached.
        result = runner.invoke(
            app,
            [
                "propose-decision",
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
                oversized,
                "--title",
                "Adopt Redis for hot caching",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "rejected"
        assert envelope["error"]["kind"] == "rejected"
        assert "exceeds" in envelope["error"]["reason"].lower()

    def test_nameless_rejected_item_rejected(self, seeded_repo) -> None:
        # Well-formed JSON, wrong keys: parse succeeds, the kernel rejects
        # at Tier 1. Caller-fixable — envelope on stdout, exit 0.
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
                "--rejected",
                '[{"title": "Memcached", "reason": "No native persistence."}]',
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "rejected"
        assert envelope["tier"] == 1
        assert "rejected[0] has no label" in envelope["assessment"]

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
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
            ],
        )
        assert result.exit_code == 1
        assert "No project found" in result.output


# ── Flag-shape coverage ─────────────────────────────────────────────────────


class TestFlagShapes:
    def test_files_affected_repeats(self, seeded_repo) -> None:
        _pid, store_path, _repo = seeded_repo
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
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
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
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
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
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
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
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
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
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
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
                "--rejected",
                '{"not": "a list"}',
            ],
        )
        assert result.exit_code == 2
        assert "--rejected" in strip_ansi(result.output)
        assert "expected JSON array of objects" in result.output

    def test_operation_bogus_exits_two_with_choices(self, seeded_repo) -> None:
        # An out-of-enum --operation is a usage error: exit 2 with the valid
        # choices named, before the adapter produces an envelope. Pairs with
        # the read-tool --mode case so the enum regression can't silently
        # return on either the read or the write autogen path.
        result = runner.invoke(
            app,
            [
                "propose-decision",
                "In-memory cache for the hot read paths across the API tier.",
                "--title",
                "Adopt Redis for hot caching",
                "--operation",
                "bogus",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "supersede" in strip_ansi(result.output)
        assert '"store"' not in result.stdout


# ── flag-question argument shapes ───────────────────────────────────────────


def _seed_resolvable(store_path: Path) -> None:
    """Seed one open question and one decision the resolve action can target."""
    (store_path / "open-questions.md").write_text("# Open Questions\n\n- [Q1] needs a decision\n")
    decisions = store_path / "decisions"
    decisions.mkdir(exist_ok=True)
    (decisions / "042-some-decision.md").write_text(active_decision_markdown(42, "Some decision"))


class TestFlagQuestionArgumentShapes:
    def test_positional_question(self, seeded_repo) -> None:
        result = runner.invoke(app, ["flag-question", "Should we cache reads?"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["store"] == "local"
        assert envelope["status"] == "ok"

    def test_question_flag_matches_positional(self, seeded_repo) -> None:
        positional = runner.invoke(app, ["flag-question", "Should we cache reads?"])
        flagged = runner.invoke(app, ["flag-question", "--question", "Should we cache writes?"])
        assert positional.exit_code == 0, positional.output
        assert flagged.exit_code == 0, flagged.output
        assert json.loads(positional.stdout) == json.loads(flagged.stdout)

    def test_both_spellings_exit_two(self, seeded_repo) -> None:
        result = runner.invoke(
            app,
            ["flag-question", "Should we cache reads?", "--question", "Should we cache writes?"],
        )
        # Usage error rendered by Typer at exit 2; the adapter never runs.
        assert result.exit_code == 2, result.output
        assert "--question" in strip_ansi(result.output)
        assert '"store"' not in result.stdout

    def test_resolved_by_without_question(self, seeded_repo) -> None:
        _pid, store_path, _repo = seeded_repo
        _seed_resolvable(store_path)
        result = runner.invoke(app, ["flag-question", "--resolved-by", "D42", "--targets", "Q1"])
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["status"] == "ok"
        assert "[Resolved by D42 on " in (store_path / "open-questions.md").read_text()
