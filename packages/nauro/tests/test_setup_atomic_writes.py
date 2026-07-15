"""Atomic-write behavior of the external config sinks in ``nauro setup``.

Seven write paths route through ``atomic_write_text``: project-scope
``.mcp.json`` add/remove, ``.claude/settings.json`` hook add/remove,
``.codex/hooks.json`` add/remove, and the user-scope ``~/.claude.json`` prune.
Each sink pins three contracts: the success path stays byte-identical to a
plain ``write_text`` of the same payload, existing permission bits survive the
rewrite, and an interrupted write leaves the original bytes intact with no
temp siblings. The primitive itself is covered in ``test_atomic.py``; these
tests cover the wiring of each sink through it.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from nauro.cli._codex_hooks import (
    _CODEX_HOOK_EVENTS,
    _CODEX_HOOK_SUBCOMMAND,
    _format_codex_hooks,
    _render_nauro_hook,
)
from nauro.cli.integrations.claude_hooks import (
    HOOK_EVENT_NAME,
    HOOK_SUBCOMMAND,
    HOOK_TIMEOUT_SECONDS,
    materialize_hooks_claude_code,
)
from nauro.cli.integrations.claude_user_config import _prune_redundant_user_scope_mcp
from nauro.cli.integrations.codex_hooks import materialize_hooks_codex
from nauro.cli.integrations.json_mcp import _configure_mcp
from nauro.cli.nauro_command import _find_nauro_command
from nauro.store import _atomic

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-shaped byte expectations; home redirection is env-based",
)

# Survivor entries: each remove case must rewrite the file (not unlink it),
# and each add case must merge into existing content.
_OTHER_MCP_SERVER = {"command": "/usr/local/bin/other", "args": ["serve"]}
_STALE_NAURO_MCP = {"command": "/x", "args": []}
_USER_HOOK_MATCHER = {"hooks": [{"type": "command", "command": "my-own-linter --check"}]}
_CODEX_USER_MATCHER = {"hooks": [{"type": "command", "command": "echo user-hook"}]}
_NAURO_SETTINGS_MATCHER = {
    "hooks": [
        {
            "type": "command",
            "command": f"nauro {HOOK_SUBCOMMAND}",
            "timeout": HOOK_TIMEOUT_SECONDS,
        }
    ]
}
_NAURO_CODEX_MATCHER = {
    "hooks": [{"type": "command", "command": f"exec nauro {_CODEX_HOOK_SUBCOMMAND}"}]
}


def _dump(obj: dict) -> str:
    """The exact serialization every JSON sink writes."""
    return json.dumps(obj, indent=2) + "\n"


def _seed_file(base: Path, rel: str, obj: dict) -> Path:
    """Seed ``base/rel`` with compact JSON so a rewrite provably reformats."""
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj), encoding="utf-8")
    return target


def _nauro_mcp_entry() -> dict:
    return {"command": _find_nauro_command(), "args": ["serve", "--stdio"]}


def _nauro_settings_hook_matcher() -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{_find_nauro_command()} {HOOK_SUBCOMMAND}",
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        ]
    }


def _nauro_codex_matcher() -> dict:
    return {"hooks": [_render_nauro_hook(_find_nauro_command())]}


@dataclass(frozen=True)
class ConfigWriteDriver:
    """One external config sink routed through ``atomic_write_text``.

    ``seed`` creates the pre-state under a base directory (the repo parent,
    doubling as ``$HOME`` for the user-scope prune) and returns the target
    path; ``run`` executes the mutation; ``expected`` builds the exact
    post-run file content. ``soft_fails`` marks the prune sink, whose
    contract is to return ``None`` on any write failure instead of raising.
    """

    id: str
    soft_fails: bool
    seed: Callable[[Path], Path]
    run: Callable[[Path], object]
    expected: Callable[[], str]


DRIVERS = [
    ConfigWriteDriver(
        id="json-mcp-add",
        soft_fails=False,
        seed=lambda base: _seed_file(
            base, "repo/.mcp.json", {"mcpServers": {"other": _OTHER_MCP_SERVER}}
        ),
        run=lambda base: _configure_mcp(base / "repo", remove=False),
        expected=lambda: _dump(
            {"mcpServers": {"other": _OTHER_MCP_SERVER, "nauro": _nauro_mcp_entry()}}
        ),
    ),
    ConfigWriteDriver(
        id="json-mcp-remove",
        soft_fails=False,
        seed=lambda base: _seed_file(
            base,
            "repo/.mcp.json",
            {"mcpServers": {"nauro": _STALE_NAURO_MCP, "other": _OTHER_MCP_SERVER}},
        ),
        run=lambda base: _configure_mcp(base / "repo", remove=True),
        expected=lambda: _dump({"mcpServers": {"other": _OTHER_MCP_SERVER}}),
    ),
    ConfigWriteDriver(
        id="settings-hook-add",
        soft_fails=False,
        seed=lambda base: _seed_file(base, "repo/.claude/settings.json", {"model": "claude-opus"}),
        run=lambda base: materialize_hooks_claude_code(base / "repo", remove=False),
        expected=lambda: _dump(
            {
                "model": "claude-opus",
                "hooks": {HOOK_EVENT_NAME: [_nauro_settings_hook_matcher()]},
            }
        ),
    ),
    ConfigWriteDriver(
        id="settings-hook-remove",
        soft_fails=False,
        seed=lambda base: _seed_file(
            base,
            "repo/.claude/settings.json",
            {
                "model": "claude-opus",
                "hooks": {HOOK_EVENT_NAME: [_USER_HOOK_MATCHER, _NAURO_SETTINGS_MATCHER]},
            },
        ),
        run=lambda base: materialize_hooks_claude_code(base / "repo", remove=True),
        expected=lambda: _dump(
            {"model": "claude-opus", "hooks": {HOOK_EVENT_NAME: [_USER_HOOK_MATCHER]}}
        ),
    ),
    ConfigWriteDriver(
        id="codex-hooks-add",
        soft_fails=False,
        seed=lambda base: _seed_file(
            base,
            "repo/.codex/hooks.json",
            {"hooks": {_CODEX_HOOK_EVENTS[0]: [_CODEX_USER_MATCHER]}},
        ),
        run=lambda base: materialize_hooks_codex(base / "repo", remove=False),
        expected=lambda: _format_codex_hooks(
            {
                "hooks": {
                    _CODEX_HOOK_EVENTS[0]: [_CODEX_USER_MATCHER, _nauro_codex_matcher()],
                    _CODEX_HOOK_EVENTS[1]: [_nauro_codex_matcher()],
                }
            }
        ),
    ),
    ConfigWriteDriver(
        id="codex-hooks-remove",
        soft_fails=False,
        seed=lambda base: _seed_file(
            base,
            "repo/.codex/hooks.json",
            {
                "hooks": {
                    _CODEX_HOOK_EVENTS[0]: [_CODEX_USER_MATCHER, _NAURO_CODEX_MATCHER],
                    _CODEX_HOOK_EVENTS[1]: [_NAURO_CODEX_MATCHER],
                }
            },
        ),
        run=lambda base: materialize_hooks_codex(base / "repo", remove=True),
        expected=lambda: _format_codex_hooks(
            {"hooks": {_CODEX_HOOK_EVENTS[0]: [_CODEX_USER_MATCHER]}}
        ),
    ),
    ConfigWriteDriver(
        id="claude-json-prune",
        soft_fails=True,
        seed=lambda base: _seed_file(
            base,
            ".claude.json",
            {
                "mcpServers": {
                    "nauro": {"type": "http", "url": "https://mcp.nauro.ai"},
                    "context7": {"command": "ctx", "args": []},
                },
                "someOtherKey": 1,
            },
        ),
        run=lambda base: _prune_redundant_user_scope_mcp(),
        expected=lambda: _dump(
            {"mcpServers": {"context7": {"command": "ctx", "args": []}}, "someOtherKey": 1}
        ),
    ),
]


@pytest.fixture(params=DRIVERS, ids=lambda driver: driver.id)
def driver(request):
    return request.param


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    """Point the home directory at ``tmp_path`` for every test.

    The prune sink writes ``Path.home() / ".claude.json"``. Both HOME and
    USERPROFILE are redirected so the developer's real file stays out of
    reach even if the module-level nt skip is ever lifted (``Path.home()``
    resolves USERPROFILE on Windows).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _failing_replace(src, dst):
    raise OSError("replace failed")


def _tmp_siblings(target: Path) -> list[str]:
    return [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]


def test_write_lands_exact_bytes(driver, tmp_path: Path):
    """The atomic path writes exactly what the plain ``write_text`` wrote."""
    target = driver.seed(tmp_path)

    driver.run(tmp_path)

    assert target.read_bytes() == driver.expected().encode("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_write_preserves_permission_bits(driver, tmp_path: Path):
    """An existing target's permission bits survive the atomic rewrite."""
    target = driver.seed(tmp_path)
    target.chmod(0o640)

    driver.run(tmp_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_interrupted_write_leaves_target_and_no_temps(driver, tmp_path: Path, monkeypatch):
    """A write that fails mid-flight leaves the original bytes and no temps.

    Same seam as ``test_atomic.py``: ``os.replace`` raising stands in for any
    failure between temp write and rename. The prune sink must additionally
    keep its soft-fail contract and report ``None`` instead of raising.
    """
    target = driver.seed(tmp_path)
    before = target.read_bytes()
    monkeypatch.setattr(_atomic.os, "replace", _failing_replace)

    if driver.soft_fails:
        assert driver.run(tmp_path) is None
    else:
        with pytest.raises(OSError, match="replace failed"):
            driver.run(tmp_path)

    assert target.read_bytes() == before
    assert _tmp_siblings(target) == []


def test_interrupted_fresh_add_leaves_no_partial_target(tmp_path: Path, monkeypatch):
    """An interrupted first write on a fresh repo leaves no partial file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_atomic.os, "replace", _failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        _configure_mcp(repo, remove=False)

    assert not (repo / ".mcp.json").exists()
    assert _tmp_siblings(repo / ".mcp.json") == []
