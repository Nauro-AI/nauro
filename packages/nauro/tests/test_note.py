"""Tests for nauro note auto-regen of AGENTS.md and input validation.

`nauro note` writes a decision or question to the store and then regenerates
AGENTS.md in every associated repo so MCP-disconnected agents see the update
without requiring a separate `nauro sync`. These tests lock in the contract.
"""

from pathlib import Path

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.agents_md import generate_agents_md
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def test_note_decision_refreshes_agents_md(tmp_path: Path, monkeypatch):
    """A decision logged via `nauro note` shows up in the repo's AGENTS.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["note", "Use Postgres for v2 storage"])
    assert result.exit_code == 0, result.output

    agents_md = repo / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    # The new decision's title appears under Recent Decisions.
    assert "Use Postgres for v2 storage" in content
    # Per-repo regen line surfaced in the user's output.
    assert "Updated AGENTS.md" in result.output
    assert str(repo) in result.output
    # Ordering: store-write summary first, then regen lines. Locks in the
    # output shape so a future refactor that reorders echoes doesn't slip by.
    assert result.output.index("Decision recorded") < result.output.index("Updated AGENTS.md")


def test_note_question_refreshes_agents_md(tmp_path: Path, monkeypatch):
    """A question logged via `nauro note` shows up under Open Questions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["note", "Should we use Postgres or SQLite?"])
    assert result.exit_code == 0, result.output

    agents_md = repo / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    assert "Should we use Postgres or SQLite?" in content
    assert "Updated AGENTS.md" in result.output


def test_note_decision_refreshes_all_associated_repos(tmp_path: Path, monkeypatch):
    """Multi-repo project: every associated repo's AGENTS.md gets refreshed."""
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    _pid, store = register_project_v2("myproj", [repo1, repo2])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo1)

    result = runner.invoke(app, ["note", "Pick GraphQL over REST"])
    assert result.exit_code == 0, result.output

    for repo in (repo1, repo2):
        agents_md = repo / "AGENTS.md"
        assert agents_md.exists(), f"AGENTS.md missing in {repo}"
        assert "Pick GraphQL over REST" in agents_md.read_text()


def test_note_preserves_unmanaged_agents_md(tmp_path: Path, monkeypatch):
    """An AGENTS.md without Nauro's markers stays byte-identical across note."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo)

    # Marker-less seed: Nauro did not generate this file, so note must not
    # rewrite it. Only `nauro sync` overwrites an unmanaged AGENTS.md.
    sentinel = b"# AGENTS.md\n\n# Manual\n\nHand-written guidance the user wrote.\n"
    (repo / "AGENTS.md").write_bytes(sentinel)

    result = runner.invoke(app, ["note", "Adopt strict TypeScript"])
    assert result.exit_code == 0, result.output

    assert (repo / "AGENTS.md").read_bytes() == sentinel
    assert "existing AGENTS.md is not Nauro-generated" in result.output
    assert "left unchanged" in result.output


def test_note_refreshes_nauro_generated_agents_md(tmp_path: Path, monkeypatch):
    """A Nauro-generated AGENTS.md is still refreshed; `# Manual` survives."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _pid, store = register_project_v2("myproj", [repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(repo)

    seeded = generate_agents_md(
        "myproj",
        "seed payload",
        manual_section="Hand-written guidance the user wrote.",
    )
    (repo / "AGENTS.md").write_text(seeded, encoding="utf-8")

    result = runner.invoke(app, ["note", "Adopt strict TypeScript"])
    assert result.exit_code == 0, result.output

    content = (repo / "AGENTS.md").read_text()
    assert "Adopt strict TypeScript" in content
    assert "Hand-written guidance the user wrote." in content
    assert "seed payload" not in content
    assert "Updated AGENTS.md" in result.output


def test_note_warns_about_missing_repo_paths(tmp_path: Path, monkeypatch):
    """Stale registry paths trigger the same warning `nauro sync` prints,
    and AGENTS.md is still written to the repos that do exist."""
    live_repo = tmp_path / "live"
    stale_repo = tmp_path / "stale"  # never mkdir'd
    live_repo.mkdir()
    _pid, store = register_project_v2("myproj", [live_repo, stale_repo])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(live_repo)

    result = runner.invoke(app, ["note", "Move billing to Stripe"])
    assert result.exit_code == 0, result.output

    # Warning surfaced for the stale path.
    assert "repo path does not exist" in result.output
    assert str(stale_repo) in result.output
    # The live repo still got its AGENTS.md refreshed.
    assert (live_repo / "AGENTS.md").exists()
    assert "Move billing to Stripe" in (live_repo / "AGENTS.md").read_text()


def test_note_question_with_rationale_warns(tmp_path: Path, monkeypatch):
    """--rationale on the question path is ignored, and the user is told so."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["note", "Should we shard the database?", "--rationale", "scaling pressure"]
    )
    assert result.exit_code == 0, result.output
    assert "Warning: --rationale/--confidence apply to decisions only" in result.output
    # Still recorded as a question, not a decision.
    assert "Question added" in result.output


def test_note_question_with_confidence_warns(tmp_path: Path, monkeypatch):
    """A non-default --confidence on the question path triggers the warning."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "--question", "Which queue to use", "-c", "high"])
    assert result.exit_code == 0, result.output
    assert "Warning: --rationale/--confidence apply to decisions only" in result.output


def test_note_both_question_and_decision_warns(tmp_path: Path, monkeypatch):
    """--question and --decision together warn that --question wins."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Pick a message broker", "--question", "--decision"])
    assert result.exit_code == 0, result.output
    assert "Warning: --question and --decision were both passed; --question wins" in result.output
    assert "Question added" in result.output


def test_note_plain_decision_no_warning(tmp_path: Path, monkeypatch):
    """The decision path with --rationale emits no flag-usage warning."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Use Postgres", "--rationale", "battle-tested"])
    assert result.exit_code == 0, result.output
    assert "Warning: --rationale/--confidence apply to decisions only" not in result.output
    assert "Warning: --question and --decision" not in result.output


def test_note_empty_string_rejects(tmp_path: Path, monkeypatch):
    """Empty text is rejected before any store write."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", ""])
    assert result.exit_code == 1
    assert "cannot be empty" in result.output
    decisions_dir = store / "decisions"
    decision_files = sorted(decisions_dir.glob("*.md"))
    assert decision_files == [decisions_dir / "001-initial-setup.md"]


def test_note_whitespace_only_rejects(tmp_path: Path, monkeypatch):
    """Whitespace-only text is rejected before any store write."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "   "])
    assert result.exit_code == 1
    assert "cannot be empty" in result.output
    decisions_dir = store / "decisions"
    decision_files = sorted(decisions_dir.glob("*.md"))
    assert decision_files == [decisions_dir / "001-initial-setup.md"]


def test_note_nonempty_text_succeeds(tmp_path: Path, monkeypatch):
    """Non-empty text records a decision — the guard does not block valid input."""
    _pid, store = register_project_v2("myproj", [tmp_path])
    scaffold_project_store("myproj", store)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["note", "Use Redis for session storage"])
    assert result.exit_code == 0, result.output
    decisions_dir = store / "decisions"
    # Count only decision documents: depending on the filelock version, the
    # write may leave <name>.lock / .lock artifacts behind in the directory.
    new_files = [f for f in decisions_dir.glob("*.md") if f.name != "001-initial-setup.md"]
    assert len(new_files) == 1
    assert "use-redis-for-session-storage" in new_files[0].name
