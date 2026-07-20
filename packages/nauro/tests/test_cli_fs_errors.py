"""Filesystem errors render as a clean message, not a raw traceback.

A read-only store, a read-only NAURO_HOME/CWD, or a full disk used to surface
as an unhandled PermissionError/OSError traceback (with absolute paths) on the
write commands and on init. They now exit 1 with a one-line "Error: ..." and no
traceback.
"""

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store.registry import register_project_v2
from nauro.templates.scaffolds import scaffold_project_store

runner = CliRunner()

# chmod-based read-only dirs don't restrict the directory owner on Windows.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX directory permissions required"
)


def _assert_clean_error(result):
    assert result.exit_code == 1, result.output
    assert "Error:" in result.output
    assert "Traceback" not in result.output
    assert "PermissionError" not in result.output


def test_note_on_read_only_store_reports_clean_error(tmp_path: Path, monkeypatch):
    _pid, store_path = register_project_v2("p", [tmp_path])
    scaffold_project_store("p", store_path)
    monkeypatch.chdir(tmp_path)

    decisions = store_path / "decisions"
    decisions.chmod(0o555)  # read-only: the write lock + file can't be created
    try:
        result = runner.invoke(app, ["note", "We chose X over Y"])
        _assert_clean_error(result)
    finally:
        decisions.chmod(0o755)  # restore so pytest can clean tmp_path


def test_init_in_read_only_cwd_reports_clean_error(tmp_path: Path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)  # can't create .nauro/config.json here
    monkeypatch.chdir(ro)
    try:
        result = runner.invoke(app, ["init", "myproj"])
        _assert_clean_error(result)
    finally:
        ro.chmod(0o755)
