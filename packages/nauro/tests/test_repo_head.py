"""Tests for ``nauro.store.repo_head.resolve_repo_head``.

Stamp-correctness cases run against real git repositories built under
``tmp_path``; fail-open cases cover every absence branch — the resolver
must return ``None`` and never raise.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from nauro.store import repo_head
from nauro.store.repo_head import resolve_process_repo_head, resolve_repo_head

# Deterministic commit identity: the autouse HOME sandbox has no gitconfig.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo_with_commit(path: Path, filename: str = "a.txt") -> str:
    """Init a repository at ``path`` with one commit; return the HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / filename).write_text("content\n")
    _git(path, "add", filename)
    _git(path, "commit", "-q", "-m", "initial")
    return _git(path, "rev-parse", "HEAD")


class TestStampCorrectness:
    def test_normal_repo_resolves_head(self, tmp_path: Path):
        sha = _repo_with_commit(tmp_path / "repo")
        assert resolve_repo_head(tmp_path / "repo") == sha

    def test_accepts_str_cwd(self, tmp_path: Path):
        sha = _repo_with_commit(tmp_path / "repo")
        assert resolve_repo_head(str(tmp_path / "repo")) == sha

    def test_subdirectory_resolves_enclosing_repo(self, tmp_path: Path):
        sha = _repo_with_commit(tmp_path / "repo")
        sub = tmp_path / "repo" / "src" / "pkg"
        sub.mkdir(parents=True)
        assert resolve_repo_head(sub) == sha

    def test_dirty_worktree_still_resolves_head(self, tmp_path: Path):
        repo = tmp_path / "repo"
        sha = _repo_with_commit(repo)
        (repo / "a.txt").write_text("uncommitted change\n")
        (repo / "untracked.txt").write_text("new\n")
        assert resolve_repo_head(repo) == sha

    def test_detached_head_resolves(self, tmp_path: Path):
        repo = tmp_path / "repo"
        first = _repo_with_commit(repo)
        (repo / "b.txt").write_text("second\n")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-q", "-m", "second")
        _git(repo, "checkout", "-q", "--detach", first)
        assert resolve_repo_head(repo) == first

    def test_linked_worktree_resolves_its_own_head(self, tmp_path: Path):
        repo = tmp_path / "repo"
        first = _repo_with_commit(repo)
        (repo / "b.txt").write_text("second\n")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-q", "-m", "second")
        second = _git(repo, "rev-parse", "HEAD")
        worktree = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "--detach", str(worktree), first)
        assert resolve_repo_head(worktree) == first
        assert resolve_repo_head(repo) == second

    def test_nested_repo_innermost_wins(self, tmp_path: Path):
        outer = tmp_path / "outer"
        outer_sha = _repo_with_commit(outer)
        inner_sha = _repo_with_commit(outer / "inner", filename="b.txt")
        assert resolve_repo_head(outer / "inner") == inner_sha
        assert resolve_repo_head(outer) == outer_sha
        assert inner_sha != outer_sha


class TestFailOpen:
    def test_none_cwd(self):
        assert resolve_repo_head(None) is None

    def test_nonexistent_cwd(self, tmp_path: Path):
        assert resolve_repo_head(tmp_path / "does-not-exist") is None

    def test_directory_outside_any_repo(self, tmp_path: Path):
        assert resolve_repo_head(tmp_path) is None

    def test_empty_repo_no_commits(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        assert resolve_repo_head(repo) is None

    def test_git_binary_absent(self, tmp_path: Path, monkeypatch):
        def raise_missing(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(repo_head.subprocess, "run", raise_missing)
        assert resolve_repo_head(tmp_path) is None

    def test_timeout_expired(self, tmp_path: Path, monkeypatch):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=2)

        monkeypatch.setattr(repo_head.subprocess, "run", raise_timeout)
        assert resolve_repo_head(tmp_path) is None

    def test_nul_byte_in_cwd(self, tmp_path: Path):
        # subprocess.run raises ValueError on an embedded NUL; the cwd is
        # client-supplied over stdio, so this must resolve to absence.
        assert resolve_repo_head(str(tmp_path) + "/x\x00y") is None

    def test_value_error_from_run(self, tmp_path: Path, monkeypatch):
        def raise_value_error(*args, **kwargs):
            raise ValueError("embedded null byte")

        monkeypatch.setattr(repo_head.subprocess, "run", raise_value_error)
        assert resolve_repo_head(tmp_path) is None

    def test_process_cwd_gone(self, monkeypatch):
        # Path.cwd() raises when the process working directory was deleted;
        # the process-level resolver must absorb it.
        def raise_missing(*args, **kwargs):
            raise FileNotFoundError("cwd deleted")

        monkeypatch.setattr(repo_head.Path, "cwd", raise_missing)
        assert resolve_process_repo_head() is None

    def test_process_resolver_delegates_to_repo_head(self, tmp_path: Path, monkeypatch):
        sha = _repo_with_commit(tmp_path / "repo")
        monkeypatch.chdir(tmp_path / "repo")
        assert resolve_process_repo_head() == sha

    def test_non_sha_stdout_is_rejected(self, tmp_path: Path, monkeypatch):
        def fake_run(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")

        monkeypatch.setattr(repo_head.subprocess, "run", fake_run)
        assert resolve_repo_head(tmp_path) is None

    def test_truncated_sha_is_rejected(self, tmp_path: Path, monkeypatch):
        def fake_run(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout="a" * 12 + "\n", stderr="")

        monkeypatch.setattr(repo_head.subprocess, "run", fake_run)
        assert resolve_repo_head(tmp_path) is None
