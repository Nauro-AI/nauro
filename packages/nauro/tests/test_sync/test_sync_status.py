"""Tests for ``nauro sync --status`` reporting.

After the legacy-transport removal, status is a two-state report:
authenticated → server URL + per-project sync info; not authenticated →
"run nauro auth login" guidance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from nauro_core.constants import MAX_BRIEF_BYTES
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.config import save_config
from nauro.store.registry import register_project_v2
from nauro.sync.state import FileState, SyncState, compute_sha256, save_state
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()


def _scaffold(name: str = "statusproj", *, repo):
    _pid, store = register_project_v2(name, [repo])
    scaffold_project_store(name, store)
    return store


class TestSyncPathReporting:
    def test_auth_token_reports_authenticated(self, tmp_path, monkeypatch):
        _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "authenticated (presign)" in result.output
        assert "Server:" in result.output

    def test_authenticated_without_project_reports_cleanly(self, tmp_path, monkeypatch):
        # Authenticated, but the cwd maps to no registered project, so
        # resolve_target_project raises typer.Exit. --status must swallow it and
        # stay a clean two-state report (exit 0), not crash with the resolver
        # error and exit 1.
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "authenticated (presign)" in result.output
        assert "Project:" not in result.output

    def test_authenticated_with_unknown_explicit_project_errors(self, tmp_path, monkeypatch):
        # An explicit --project that does not resolve is a real error, not the
        # ambient no-project case: the nonzero exit must agree with the resolver
        # error rather than being swallowed to exit 0.
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status", "--project", "does-not-exist"])
        assert result.exit_code == 1, result.output
        assert "authenticated (presign)" in result.output

    def test_no_credentials_reports_not_authenticated(self, tmp_path, monkeypatch):
        save_config({})
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "not authenticated" in result.output
        assert "nauro auth login" in result.output


class TestLastSyncTime:
    def test_reports_last_full_sync_when_present(self, tmp_path, monkeypatch):
        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        stamp = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        state = SyncState(last_full_sync=stamp)
        save_state(store, state)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert stamp in result.output

    def test_reports_never_when_absent(self, tmp_path, monkeypatch):
        _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Last successful sync: never" in result.output


class TestPendingLocalChanges:
    def test_status_matches_push_eligible_existing_files(self, tmp_path, monkeypatch):
        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)

        state = SyncState()
        for path in store.rglob("*"):
            if path.is_file():
                relative_path = str(path.relative_to(store))
                state.files[relative_path] = FileState(
                    local_sha256=compute_sha256(path),
                    remote_etag='"synced"',
                    last_sync="2026-05-16T00:00:00Z",
                )

        deleted = store / "deleted.md"
        deleted.write_text("gone\n")
        state.files["deleted.md"] = FileState(
            local_sha256=compute_sha256(deleted),
            remote_etag='"synced"',
            last_sync="2026-05-16T00:00:00Z",
        )
        deleted.unlink()
        save_state(store, state)

        (store / "stack.md").write_text("modified\n")
        context_dir = store / "context"
        context_dir.mkdir()
        (context_dir / "new.md").write_text("new\n")
        (context_dir / "too-large.md").write_text("x" * (MAX_BRIEF_BYTES + 1))
        (context_dir / "draft.md.lock").write_text("local lock\n")
        journal_dir = store / "journal"
        journal_dir.mkdir()
        (journal_dir / "events.jsonl").write_text("{}\n")
        scratch_dir = store / ".pull-spool-dead"
        scratch_dir.mkdir()
        (scratch_dir / "000000").write_text("scratch\n")
        backup_dir = store / ".conflict-backup"
        backup_dir.mkdir()
        (backup_dir / "loser.md").write_text("backup\n")
        (store / "nauro-graph.html").write_text("generated\n")

        result = runner.invoke(app, ["sync", "--status"])

        assert result.exit_code == 0, result.output
        assert "Pending local changes: 2" in result.output
        assert "    - stack.md" in result.output
        assert "    - context/new.md" in result.output
        for excluded in (
            "deleted.md",
            "context/too-large.md",
            "context/draft.md.lock",
            "journal/events.jsonl",
            ".pull-spool-dead/000000",
            ".conflict-backup/loser.md",
            "nauro-graph.html",
        ):
            assert excluded not in result.output
        assert "not pushed" not in result.output

    def test_status_reports_unknown_and_continues_when_store_scan_fails(
        self, tmp_path, monkeypatch
    ):
        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)
        backup_dir = store / ".conflict-backup"
        backup_dir.mkdir()
        (backup_dir / "loser.md").write_text("backup\n")

        with patch(
            "nauro.sync.push.compute_sha256",
            side_effect=FileNotFoundError("file disappeared during scan"),
        ):
            result = runner.invoke(app, ["sync", "--status"])

        assert result.exit_code == 0, result.output
        assert (
            "Pending local changes: unknown "
            "(store scan failed: file disappeared during scan)" in result.output
        )
        assert "Conflict backups: 1" in result.output


class TestQuarantinedCollisions:
    def test_status_names_each_unresolved_collision(self, tmp_path, monkeypatch):
        from nauro.sync.quarantine import save_quarantine_backup

        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)
        save_quarantine_backup(store, "decisions/003-remote.md", b"remote body\n", '"etag"')

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Quarantined decision-number collisions: 1" in result.output
        assert "decisions/003-remote.md" in result.output

    def test_quarantine_backups_are_not_counted_twice(self, tmp_path, monkeypatch):
        """The quarantine copy lives in the conflict-backup directory, so the
        generic total must exclude what the line above already named."""
        from nauro.sync.merge import write_backup
        from nauro.sync.quarantine import save_quarantine_backup

        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)
        save_quarantine_backup(store, "decisions/003-remote.md", b"remote body\n", '"etag"')

        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Quarantined decision-number collisions: 1" in result.output
        assert "Conflict backups" not in result.output

        write_backup(store, "20260810T000000Z-project.md", b"losing side\n")
        result = runner.invoke(app, ["sync", "--status"])
        assert result.exit_code == 0, result.output
        assert "Conflict backups: 1" in result.output

    def test_an_orphaned_tmp_sibling_is_not_counted(self, tmp_path, monkeypatch):
        """A kill between the tmp write and the replace strands a sibling.

        It lands in the same directory as the backups, under a name nothing
        reads. It is not a backup, and reporting it as one would tell the user
        a conflict happened that did not.
        """
        from nauro.store import _atomic
        from nauro.sync.merge import write_backup

        store = _scaffold(repo=tmp_path)
        save_config(
            {
                "auth": {
                    "sub": "auth0|test",
                    "access_token": "tok_orig",
                    "refresh_token": "refresh_orig",
                }
            }
        )
        monkeypatch.chdir(tmp_path)
        with patch.object(_atomic.os, "replace", lambda src, dst: None):
            write_backup(store, "20260810T000000Z-project.md", b"losing side\n")
        assert [path.name for path in (store / ".conflict-backup").iterdir()] != []

        result = runner.invoke(app, ["sync", "--status"])

        assert result.exit_code == 0, result.output
        assert "Conflict backups" not in result.output
