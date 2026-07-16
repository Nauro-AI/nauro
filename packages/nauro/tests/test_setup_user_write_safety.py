"""Tests for user-global write safety in ``nauro setup``.

Two guarantees for files under the user's home directory:

1. ``~/.codex/config.toml`` is hand-maintained user config: edits preserve
   comments, formatting, and user-added keys (tomlkit), only mutate the
   ``command``/``args`` keys Nauro owns, skip the write entirely when nothing
   would change, and go through the atomic writer (permission bits survive).
2. A user-global final target that is itself a symlink is refused (a dotfile
   manager may own the real file), while symlinked parent directories keep
   working (dotfile managers routinely symlink whole config directories).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import tomlkit

from nauro.cli.integrations.agents import materialize_agents
from nauro.cli.integrations.claude_user_config import _prune_redundant_user_scope_mcp
from nauro.cli.integrations.codex_config import _configure_codex
from nauro.cli.integrations.outcomes import (
    AgentKind,
    ClaudeUserConfigKind,
    CodexConfigKind,
    SkillKind,
)
from nauro.cli.integrations.skills import _materialize_skill_file, _remove_skill_file
from nauro.cli.nauro_command import _find_nauro_command

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

symlinks_required = pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra Windows privileges"
)

# A commented, oddly formatted config with a pre-existing [mcp_servers] header
# and a sibling entry. Every byte outside the nauro entry must survive edits.
COMMENTED_SEED = (
    "# Codex configuration\n"
    'model = "gpt-5"        # pinned\n'
    "\n"
    "# my servers\n"
    "[mcp_servers]\n"
    "\n"
    "[mcp_servers.other]\n"
    'command = "other-cmd"   # keep\n'
    'args = [ "a",   "b" ]\n'
)

MTIME_SENTINEL = 1_000_000_000


def _seed_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(content, encoding="utf-8")
    return config_path


# ─── formatting preservation (tomlkit) ───────────────────────────────────────


def test_codex_add_preserves_commented_config_outside_nauro_entry(tmp_path: Path):
    config_path = _seed_config(tmp_path, COMMENTED_SEED)

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    # Everything before the appended entry is byte-identical to the seed.
    assert text.startswith(COMMENTED_SEED)
    assert "[mcp_servers.nauro]" in text[len(COMMENTED_SEED) :]
    data = tomllib.loads(text)
    assert data["mcp_servers"]["other"]["args"] == ["a", "b"]
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]


def test_codex_add_then_remove_is_byte_identical_with_existing_table(tmp_path: Path):
    config_path = _seed_config(tmp_path, COMMENTED_SEED)

    _configure_codex(remove=False, config_path=config_path)
    msg = _configure_codex(remove=True, config_path=config_path)

    assert msg.kind is CodexConfigKind.REMOVED
    assert config_path.read_bytes() == COMMENTED_SEED.encode("utf-8")


def test_codex_add_then_remove_on_tableless_file_leaves_empty_header(tmp_path: Path):
    original = '# hello\nmodel = "gpt-5"\n'
    config_path = _seed_config(tmp_path, original)

    _configure_codex(remove=False, config_path=config_path)
    _configure_codex(remove=True, config_path=config_path)

    # The parent table was created by the add and is deliberately not popped
    # on remove; all pre-existing content is byte-identical.
    assert config_path.read_text(encoding="utf-8") == original + "\n[mcp_servers]\n"


def test_codex_second_identical_add_skips_render_and_write(tmp_path: Path):
    config_path = tmp_path / ".codex" / "config.toml"
    _configure_codex(remove=False, config_path=config_path)
    before = config_path.read_bytes()
    os.utime(config_path, (MTIME_SENTINEL, MTIME_SENTINEL))

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.ALREADY_CONFIGURED
    assert msg.config_path == config_path
    assert config_path.read_bytes() == before
    assert config_path.stat().st_mtime == MTIME_SENTINEL


def test_codex_remove_without_entry_leaves_commented_file_untouched(tmp_path: Path):
    config_path = _seed_config(tmp_path, COMMENTED_SEED)
    os.utime(config_path, (MTIME_SENTINEL, MTIME_SENTINEL))

    msg = _configure_codex(remove=True, config_path=config_path)

    assert msg.kind is CodexConfigKind.NOTHING_TO_REMOVE
    assert config_path.read_bytes() == COMMENTED_SEED.encode("utf-8")
    assert config_path.stat().st_mtime == MTIME_SENTINEL


def test_codex_add_refuses_invalid_utf8_without_writing(tmp_path: Path):
    raw = b'\xff\xfemodel = "x"\n'
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_bytes(raw)

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.PARSE_ERROR_UTF8
    assert msg.config_path == config_path
    assert config_path.read_bytes() == raw


def test_codex_remove_refuses_invalid_utf8_without_writing(tmp_path: Path):
    raw = b'\xff\xfemodel = "x"\n'
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_bytes(raw)

    msg = _configure_codex(remove=True, config_path=config_path)

    assert msg.kind is CodexConfigKind.PARSE_ERROR_UTF8
    assert msg.config_path == config_path
    assert config_path.read_bytes() == raw


def test_codex_new_entry_renders_as_standard_table_not_inline(tmp_path: Path):
    config_path = tmp_path / ".codex" / "config.toml"

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.nauro]" in text
    assert "nauro = {" not in text
    data = tomllib.loads(text)
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]


def test_codex_command_drift_rewrite_preserves_user_keys_and_comments(tmp_path: Path):
    """Targeted key update: a drifted command is rewritten in place, and the
    user's own keys and comments inside the nauro entry survive."""
    seed = (
        "[mcp_servers.nauro]\n"
        "# tuned for my machine\n"
        'command = "/old/dead/venv/bin/nauro"\n'
        'args = ["serve", "--stdio"]\n'
        "startup_timeout_ms = 20000\n"
    )
    config_path = _seed_config(tmp_path, seed)

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    assert "# tuned for my machine" in text
    entry = tomllib.loads(text)["mcp_servers"]["nauro"]
    assert entry["startup_timeout_ms"] == 20000
    assert entry["command"] == _find_nauro_command()
    assert entry["command"] != "/old/dead/venv/bin/nauro"
    assert entry["args"] == ["serve", "--stdio"]


def test_codex_command_only_drift_leaves_matching_args_bytes_untouched(tmp_path: Path):
    """Per-key update: when only the command drifted, an args array whose
    value already matches is not rewritten, so its multiline layout and
    inline comment survive byte-for-byte."""
    args_block = 'args = [\n  "serve", # transport pinned\n  "--stdio",\n]\n'
    seed = "[mcp_servers.nauro]\n" + 'command = "/old/dead/venv/bin/nauro"\n' + args_block
    config_path = _seed_config(tmp_path, seed)

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    assert args_block in text
    entry = tomlkit.parse(text)["mcp_servers"]["nauro"]
    assert entry["command"] == _find_nauro_command()
    assert list(entry["args"]) == ["serve", "--stdio"]


# ─── inline-table mcp_servers parents ────────────────────────────────────────


# An inline `mcp_servers = { ... }` is valid hand-written TOML. Nesting a
# block table inside it renders invalid TOML, so entry creation must match
# the user's inline style. Every test reparses the written file with tomlkit
# to prove the config was not corrupted.


def test_codex_add_into_empty_inline_mcp_servers(tmp_path: Path):
    config_path = _seed_config(tmp_path, "mcp_servers = {}\n")

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    reparsed = tomlkit.parse(text)
    assert reparsed["mcp_servers"]["nauro"]["command"] == _find_nauro_command()
    assert list(reparsed["mcp_servers"]["nauro"]["args"]) == ["serve", "--stdio"]
    # The parent kept its inline style: no [mcp_servers] block header appeared.
    assert text.startswith("mcp_servers = {")
    assert "[mcp_servers" not in text


def test_codex_add_into_inline_mcp_servers_preserves_sibling(tmp_path: Path):
    seed = 'mcp_servers = { other = { command = "c", args = [] } }\n'
    config_path = _seed_config(tmp_path, seed)

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    assert '{ command = "c", args = [] }' in text
    reparsed = tomlkit.parse(text)
    assert reparsed["mcp_servers"]["other"]["command"] == "c"
    assert list(reparsed["mcp_servers"]["other"]["args"]) == []
    assert reparsed["mcp_servers"]["nauro"]["command"] == _find_nauro_command()
    assert list(reparsed["mcp_servers"]["nauro"]["args"]) == ["serve", "--stdio"]


def test_codex_add_replaces_non_table_nauro_inside_inline_parent(tmp_path: Path):
    config_path = _seed_config(tmp_path, 'mcp_servers = { nauro = "weird" }\n')

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    text = config_path.read_text(encoding="utf-8")
    reparsed = tomlkit.parse(text)
    assert reparsed["mcp_servers"]["nauro"]["command"] == _find_nauro_command()
    assert list(reparsed["mcp_servers"]["nauro"]["args"]) == ["serve", "--stdio"]
    assert "[mcp_servers" not in text


def test_codex_add_then_remove_on_inline_mcp_servers_round_trips(tmp_path: Path):
    seed = "mcp_servers = {}\n"
    config_path = _seed_config(tmp_path, seed)

    _configure_codex(remove=False, config_path=config_path)
    msg = _configure_codex(remove=True, config_path=config_path)

    assert msg.kind is CodexConfigKind.REMOVED
    text = config_path.read_text(encoding="utf-8")
    reparsed = tomlkit.parse(text)
    assert "nauro" not in reparsed["mcp_servers"]
    # The inline parent survives the round trip byte-for-byte.
    assert text == seed


# ─── symlink refusal on user-global final targets ────────────────────────────


@symlinks_required
def test_codex_add_refuses_symlinked_config(tmp_path: Path):
    real = tmp_path / "real-config.toml"
    seed = '[mcp_servers.other]\ncommand = "c"\nargs = []\n'
    real.write_text(seed, encoding="utf-8")
    link = tmp_path / ".codex" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)

    msg = _configure_codex(remove=False, config_path=link)

    assert msg.kind is CodexConfigKind.REFUSED_SYMLINK
    assert msg.refusal.target == link
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == seed


@symlinks_required
def test_codex_remove_refuses_symlinked_config(tmp_path: Path):
    real = tmp_path / "real-config.toml"
    seed = '[mcp_servers.nauro]\ncommand = "nauro"\nargs = ["serve", "--stdio"]\n'
    real.write_text(seed, encoding="utf-8")
    link = tmp_path / ".codex" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)

    msg = _configure_codex(remove=True, config_path=link)

    assert msg.kind is CodexConfigKind.REFUSED_SYMLINK
    assert msg.refusal.target == link
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == seed


@symlinks_required
def test_codex_writes_through_symlinked_parent_dir(tmp_path: Path):
    """A symlinked ~/.codex directory is the dotfile-manager layout and must
    keep working: the write lands through the resolved directory."""
    real_dir = tmp_path / "dotfiles" / "codex"
    real_dir.mkdir(parents=True)
    codex_dir = tmp_path / ".codex"
    codex_dir.symlink_to(real_dir, target_is_directory=True)
    config_path = codex_dir / "config.toml"

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    assert codex_dir.is_symlink()
    with (real_dir / "config.toml").open("rb") as f:
        data = tomllib.load(f)
    assert data["mcp_servers"]["nauro"]["args"] == ["serve", "--stdio"]


@symlinks_required
def test_prune_skips_symlinked_claude_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "dotfiles-claude.json"
    raw = json.dumps({"mcpServers": {"nauro": {"type": "http", "url": "https://mcp.nauro.ai"}}})
    real.write_text(raw, encoding="utf-8")
    (tmp_path / ".claude.json").symlink_to(real)

    msg = _prune_redundant_user_scope_mcp()

    assert msg is not None
    assert msg.kind is ClaudeUserConfigKind.REFUSED_SYMLINK
    assert (tmp_path / ".claude.json").is_symlink()
    assert real.read_text(encoding="utf-8") == raw


@symlinks_required
def test_skill_add_refuses_symlinked_target(tmp_path: Path):
    real = tmp_path / "real-skill.md"
    real.write_text("original", encoding="utf-8")
    target = tmp_path / "skills" / "nauro-adopt" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(real)

    line = _materialize_skill_file(target, "new body")

    assert line.kind is SkillKind.REFUSED_SYMLINK
    assert target.is_symlink()
    assert real.read_text(encoding="utf-8") == "original"


@symlinks_required
def test_skill_remove_refuses_symlinked_target(tmp_path: Path):
    real = tmp_path / "real-skill.md"
    real.write_text("original", encoding="utf-8")
    base = tmp_path / "skills"
    target = base / "nauro-adopt" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(real)

    line = _remove_skill_file(target, stop_above=base)

    assert line.kind is SkillKind.REFUSED_SYMLINK
    assert target.is_symlink()
    assert real.read_text(encoding="utf-8") == "original"


@symlinks_required
def test_skill_writes_through_symlinked_parent_dir(tmp_path: Path):
    real_dir = tmp_path / "dotfiles" / "skills"
    real_dir.mkdir(parents=True)
    base = tmp_path / "skills"
    base.symlink_to(real_dir, target_is_directory=True)
    target = base / "nauro-adopt" / "SKILL.md"

    line = _materialize_skill_file(target, "body")

    assert line.kind is SkillKind.WROTE
    assert line.target == target
    assert base.is_symlink()
    assert (real_dir / "nauro-adopt" / "SKILL.md").read_text(encoding="utf-8") == "body"


@symlinks_required
def test_agent_materialization_refuses_symlinked_target(tmp_path: Path, monkeypatch):
    from nauro.agents import AGENT_NAMES

    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / ".claude" / "agents"
    base.mkdir(parents=True)
    real = tmp_path / "real-agent.md"
    real.write_text("mine", encoding="utf-8")
    linked = base / f"{AGENT_NAMES[0]}.md"
    linked.symlink_to(real)

    add_lines = materialize_agents("claude_code", remove=False)

    add_refusals = [line for line in add_lines if line.kind is AgentKind.REFUSED_SYMLINK]
    assert len(add_refusals) == 1
    assert add_refusals[0].refusal.target == linked
    assert linked.is_symlink()
    assert real.read_text(encoding="utf-8") == "mine"
    for name in AGENT_NAMES[1:]:
        assert (base / f"{name}.md").is_file()

    remove_lines = materialize_agents("claude_code", remove=True)

    remove_refusals = [line for line in remove_lines if line.kind is AgentKind.REFUSED_SYMLINK]
    assert len(remove_refusals) == 1
    assert linked.is_symlink()
    assert real.read_text(encoding="utf-8") == "mine"


# ─── permission preservation through the atomic write ───────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_codex_write_preserves_permission_bits(tmp_path: Path):
    seed = '[mcp_servers.nauro]\ncommand = "/stale/nauro"\nargs = ["serve", "--stdio"]\n'
    config_path = _seed_config(tmp_path, seed)
    config_path.chmod(0o640)

    msg = _configure_codex(remove=False, config_path=config_path)

    assert msg.kind is CodexConfigKind.WROTE
    assert oct(config_path.stat().st_mode & 0o777) == "0o640"
