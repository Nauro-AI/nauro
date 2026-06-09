"""`nauro init` refuses repo roots whose ``.nauro/config.json`` is the global config.

With the default home layout that is the home directory itself:
``repo_config_path($HOME)`` and the global config are the same file, and that
file holds auth tokens and telemetry consent. Before this guard, ``nauro init
--demo`` run from $HOME exited with a misleading "Re-run with --force" hint,
and obeying it replaced the global config with a demo project pointer. The
refusal fires before any registry or store mutation and is force-proof.

CWD and NAURO_HOME are isolated by autouse conftest fixtures; each test
re-points NAURO_HOME at a ``<home>/.nauro`` layout to replicate production.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.store import registry

runner = CliRunner()

SENTINEL = json.dumps({"auth": {"access_token": "keep-me"}}, indent=2) + "\n"


def _home_layout(tmp_path, monkeypatch):
    """Replicate the production default: global config at ``<home>/.nauro/config.json``."""
    home = tmp_path / "home"
    nauro_home = home / ".nauro"
    nauro_home.mkdir(parents=True)
    monkeypatch.setenv("NAURO_HOME", str(nauro_home))
    global_config = nauro_home / "config.json"
    global_config.write_text(SENTINEL)
    monkeypatch.chdir(home)
    return home, global_config


def _assert_global_config_intact(global_config):
    """The global config kept its auth block and did not become a repo config.

    Telemetry consent bookkeeping (``anonymous_id``) is merged into the global
    config by the app callback on any CLI run; that merge is fine. What must
    not happen is the repo-config replacement: auth gone, ``mode``/``id`` keys
    present.
    """
    data = json.loads(global_config.read_text())
    assert data["auth"] == {"access_token": "keep-me"}
    assert "mode" not in data
    assert "id" not in data


def test_init_demo_from_home_is_refused(tmp_path, monkeypatch):
    """--demo from the home directory aborts without touching any state."""
    _home, global_config = _home_layout(tmp_path, monkeypatch)

    result = runner.invoke(app, ["init", "--demo"])

    assert result.exit_code == 1
    assert "global config" in result.output
    assert "Re-run with --force" not in result.output
    _assert_global_config_intact(global_config)
    assert registry.find_projects_by_name_v2("demo-project") == []


def test_init_demo_force_from_home_is_still_refused(tmp_path, monkeypatch):
    """--force must not bypass the guard; bypassing destroyed auth and consent."""
    _home, global_config = _home_layout(tmp_path, monkeypatch)

    result = runner.invoke(app, ["init", "--demo", "--force"])

    assert result.exit_code == 1
    _assert_global_config_intact(global_config)
    assert registry.find_projects_by_name_v2("demo-project") == []


def test_plain_init_from_home_is_refused(tmp_path, monkeypatch):
    """Non-demo init from the home directory is refused the same way."""
    _home, global_config = _home_layout(tmp_path, monkeypatch)

    result = runner.invoke(app, ["init", "someproject"])

    assert result.exit_code == 1
    assert "global config" in result.output
    _assert_global_config_intact(global_config)
    assert registry.find_projects_by_name_v2("someproject") == []


def test_init_add_repo_pointing_at_home_is_refused(tmp_path, monkeypatch):
    """--add-repo <home> is refused even when the cwd is elsewhere."""
    home, global_config = _home_layout(tmp_path, monkeypatch)
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(app, ["init", "someproject", "--add-repo", str(home)])

    assert result.exit_code == 1
    assert "global config" in result.output
    _assert_global_config_intact(global_config)
    assert registry.find_projects_by_name_v2("someproject") == []


def test_init_demo_in_ordinary_dir_still_works(tmp_path, monkeypatch):
    """The guard does not catch normal directories under the same home."""
    home, _global_config = _home_layout(tmp_path, monkeypatch)
    project_dir = home / "nauro-demo"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    result = runner.invoke(app, ["init", "--demo"])

    assert result.exit_code == 0, result.output
    assert (project_dir / ".nauro" / "config.json").is_file()
