"""Tests for nauro.store.home — the single resolution of the Nauro home path."""

from pathlib import Path

from nauro.store.home import (
    config_file,
    ensure_nauro_home,
    nauro_home,
    projects_dir,
    registry_file,
)


def test_env_var_overrides_the_home(tmp_path, monkeypatch):
    """``NAURO_HOME`` wins over the default location."""
    override = tmp_path / "elsewhere"
    monkeypatch.setenv("NAURO_HOME", str(override))
    assert nauro_home() == override


def test_default_home_is_dot_nauro_under_the_user_home(tmp_path, monkeypatch):
    """Without the env var the home is ``~/.nauro``."""
    monkeypatch.delenv("NAURO_HOME", raising=False)
    assert nauro_home() == Path.home() / ".nauro"


def test_ensure_creates_the_home_owner_only(tmp_path, monkeypatch):
    """A fresh home is created at 0o700 so the token and store stay private."""
    home = tmp_path / "fresh_home"
    monkeypatch.setenv("NAURO_HOME", str(home))
    assert ensure_nauro_home() == home
    assert oct(home.stat().st_mode & 0o777) == "0o700"


def test_ensure_keeps_an_existing_home_and_tightens_it(tmp_path, monkeypatch):
    """An existing home keeps its contents; a group/other-accessible mode is fixed."""
    home = tmp_path / "wide_home"
    home.mkdir()
    home.chmod(0o755)
    (home / "config.json").write_text("{}")
    monkeypatch.setenv("NAURO_HOME", str(home))
    ensure_nauro_home()
    assert (home / "config.json").read_text() == "{}"
    assert (home.stat().st_mode & 0o077) == 0


def test_named_paths_are_children_of_the_home(tmp_path, monkeypatch):
    """Every named path resolves under the active home."""
    home = tmp_path / "home_root"
    monkeypatch.setenv("NAURO_HOME", str(home))
    for path in (registry_file(), projects_dir(), config_file()):
        assert path.parent == home
