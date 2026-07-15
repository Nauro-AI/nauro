"""nauro setup — Configure tool integrations.

Subcommands:
  nauro setup claude-code  — register MCP server in <repo>/.mcp.json (project scope)
                             for each of the project's repos
  nauro setup cursor       — register MCP server in <repo>/.cursor/mcp.json
                             for each of the project's repos
  nauro setup codex        — register MCP server in ~/.codex/config.toml
"""

from __future__ import annotations

import functools
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import tomlkit
import typer
from tomlkit.exceptions import ParseError as TOMLParseError
from tomlkit.items import InlineTable

from nauro.cli import utils as cli_utils
from nauro.cli._codex_hooks import (
    _CODEX_HOOK_PROBE_ARGS,
    _CodexHookConfigError,
    _format_codex_hooks,
    _parse_codex_hooks,
    _transform_codex_hooks,
    _validate_codex_hooks,
)
from nauro.cli.git_hygiene import public_surface_git_warnings
from nauro.cli.integrations.agents import materialize_agents
from nauro.cli.integrations.claude_user_config import _prune_redundant_user_scope_mcp
from nauro.cli.integrations.legacy import _remove_claude_md
from nauro.cli.integrations.skills import (
    materialize_skills_claude_code,
    materialize_skills_codex,
    materialize_skills_cursor_for_repo,
)
from nauro.cli.integrations.user_scope import _registered_project_keys, _user_scope_safe_to_clear
from nauro.cli.utils import _resolve_project_entry, resolve_target_project
from nauro.store._atomic import atomic_write_text
from nauro.store.registry import get_repo_paths
from nauro.store.resolution import resolve_from_cwd
from nauro.store.write_safety import find_file_symlink, find_symlink
from nauro.templates.agents_md import remove_generated_agents_md
from nauro.templates.agents_md_regen import warn_then_regen

setup_app = typer.Typer(help="Configure tool integrations.")

# Discoverability hint appended to every setup-add success path.
# `nauro check-decision` (the L1 surface) works from the current shell
# against the local store, so users don't have to wait for an agent
# restart to see Nauro do something useful.
CHECK_HINT_LINE = 'Try it now from this shell: nauro check-decision "<approach>"'


def _interpreter_sibling_candidate() -> str | None:
    """Return the absolute path to a ``nauro`` console script next to the running
    interpreter, or None when there isn't one.

    This is the install the user actually invoked, which pipx/uv-tool layouts
    keep off the PATH that GUI-launched agents see — recording its absolute path
    is what makes the spawned stdio server and per-turn hook independent of the
    agent's launch environment.
    """
    bindir = Path(sys.executable).parent
    for name in ("nauro", "nauro.exe"):
        candidate = bindir / name
        if candidate.is_file():
            return str(candidate)
    return None


_FRAGILE_COMMAND_WARNING = (
    "WARNING: recording nauro from a project virtualenv ({command}).\n"
    "  This path breaks if the repo's virtualenv is rebuilt, moved, or "
    "corrupted, silently killing Nauro's MCP server and hooks. Install nauro "
    "durably (pipx install nauro, or uv tool install nauro) and re-run "
    "'nauro setup all'."
)

_UNRESOLVED_COMMAND_WARNING = (
    "WARNING: could not validate a working nauro; recorded '{command}'.\n"
    "  Nauro's MCP server and hooks will not work until nauro is installed on a "
    "durable PATH (pipx install nauro, or uv tool install nauro), then re-run "
    "'nauro setup all'."
)


@functools.cache
def _find_nauro_command() -> str:
    """Resolve — and cache for the process — the nauro entrypoint recorded into
    MCP and hook configs.

    Cached so `setup all` validates the entrypoint once rather than once per
    sink (five subprocess probes collapse to one). Warnings surface on the
    cache-miss resolution only; tests reset via
    ``_find_nauro_command.cache_clear()``.
    """
    return _resolve_nauro_command()


def _resolve_nauro_command() -> str:
    """Pick the nauro entrypoint to record into MCP/hook configs.

    Prefers a validated, durable install so the recorded command keeps working
    after a project virtualenv is rebuilt, moved, or corrupted (the observed
    failure: a ``uv run`` / ``.venv``-invoked setup recorded a fragile
    repo-venv path that later died). Resolution order:

      1. Interpreter-sibling that both runs and looks durable — the fast path,
         byte-identical to the historical behavior for pipx/uv-tool/desktop.
      2. Otherwise a PATH-resolved absolute shim that runs and looks durable —
         diverts away from a dead or fragile project venv.
      3. Otherwise the sibling if it merely runs (fragile but working) —
         recorded with a loud warning naming the project-venv fragility.
      4. Otherwise the best absolute path we have (else bare ``nauro``), with a
         loud warning that MCP will not work until nauro is on a durable PATH.

    An absolute path is always preferred over bare ``nauro``; bare ``nauro`` is
    only the terminal fallback, because GUI-launched agents start with an empty
    PATH. Durability checks run before the (subprocess) probe so a non-durable
    candidate short-circuits without spawning.
    """
    sibling = _interpreter_sibling_candidate()
    which = shutil.which("nauro")

    if (
        sibling is not None
        and cli_utils._is_durable_install_path(sibling)
        and cli_utils.probe_nauro_command(sibling)
    ):
        return sibling

    if (
        which is not None
        and cli_utils._is_durable_install_path(which)
        and cli_utils.probe_nauro_command(which)
    ):
        return which

    if sibling is not None and cli_utils.probe_nauro_command(sibling):
        typer.echo(_FRAGILE_COMMAND_WARNING.format(command=sibling), err=True)
        return sibling

    fallback = sibling or which or "nauro"
    typer.echo(_UNRESOLVED_COMMAND_WARNING.format(command=fallback), err=True)
    return fallback


def _configure_json_mcp(
    repo_path: Path,
    *,
    config_rel_path: str,
    label: str,
    remove: bool,
) -> str:
    """Add or remove the Nauro MCP entry in a JSON config file at ``repo_path / config_rel_path``.

    Shared shape behind ``_configure_mcp`` (``.mcp.json``) and
    ``_configure_cursor_for_repo`` (``.cursor/mcp.json``): load → parse →
    mutate ``mcpServers["nauro"]`` → write. Both surfaces use the same key
    name and entry shape, so the only per-surface variation is the relative
    path and the human-readable ``label`` used in status messages.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    refusal = find_symlink(repo_path, config_rel_path)
    if refusal is not None:
        return f"  {repo_path}: {refusal.message}"
    config_path = repo_path / config_rel_path
    nauro_cmd = _find_nauro_command()
    nauro_entry = {"command": nauro_cmd, "args": ["serve", "--stdio"]}

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"  {repo_path}: could not parse {label} - {exc}"
    else:
        config = {}

    # A hand-mangled config can have a non-object top level (e.g. a JSON array)
    # or an mcpServers that isn't an object; mutating it would raise. Skip with a
    # clear message instead of a traceback, mirroring the hook path's guard.
    if not isinstance(config, dict):
        return f"  {repo_path}: {label} is not a JSON object, skipped"

    if remove:
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict) or "nauro" not in servers:
            return f"  {repo_path}: no nauro entry to remove"
        del servers["nauro"]
        if not servers:
            config.pop("mcpServers", None)
        if config:
            atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")
        else:
            config_path.unlink()
        return f"  {repo_path}: removed nauro from {label}"

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return f"  {repo_path}: mcpServers in {label} is not a JSON object, skipped"
    servers["nauro"] = nauro_entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config_path, json.dumps(config, indent=2) + "\n")
    lines = [f"  {repo_path}: wrote nauro to {label}"]
    lines.extend(public_surface_git_warnings(repo_path, config_rel_path))
    return "\n".join(lines)


def _configure_mcp(repo_path: Path, *, remove: bool = False) -> str:
    """Add or remove the Nauro MCP entry in the repo's project-scope ``.mcp.json``.

    Writes the file directly. Mirrors how ``_configure_cursor_for_repo``
    handles ``.cursor/mcp.json`` and ``_configure_codex`` handles
    ``~/.codex/config.toml``, so all three surface handlers share one shape.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".mcp.json",
        label=".mcp.json",
        remove=remove,
    )


@setup_app.command(name="claude-code")
def claude_code(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's advisory UserPromptSubmit hook into each repo's "
            "project-scope .claude/settings.json. The hook surfaces related "
            "decisions as context each turn and never blocks."
        ),
    ),
) -> None:
    """Configure Claude Code to use Nauro during sessions."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    # Clean up legacy CLAUDE.md blocks (behavioral guidance now delivered
    # via MCP server instructions, so the injected block is no longer needed).
    legacy_results = []
    mcp_results = []
    hook_results = []
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            mcp_results.append(f"  {repo_path}: repo path missing, skipped")
            continue
        legacy = _remove_claude_md(repo_path)
        if legacy:
            legacy_results.append(legacy)
        mcp_results.append(_configure_mcp(repo_path, remove=remove))
        if with_hooks or remove:
            try:
                hook_results.append(materialize_hooks_claude_code(repo_path, remove=remove))
            except Exception as exc:
                hook_results.append(f"  {repo_path}: hook wiring error - {exc}")

    if not remove:
        pruned = _prune_redundant_user_scope_mcp()
        if pruned:
            mcp_results.append(pruned)

    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}':\n")
    for line in mcp_results:
        typer.echo(line)

    if hook_results:
        typer.echo("\nHooks:")
        for line in hook_results:
            typer.echo(line)

    if legacy_results:
        typer.echo("\nLegacy cleanup:")
        for line in legacy_results:
            typer.echo(line)

    if not remove:
        # Regenerate AGENTS.md so context is fresh from the start. The store
        # dir name is the v2 id (or v1 name) used by the registry-aware lookup.
        # warn_then_regen surfaces missing-repo, symlink-refusal, and
        # git-hygiene warnings through the warn callback.
        updated_repos = warn_then_regen(
            _store_path.name,
            _store_path,
            warn=lambda msg: typer.echo(msg, err=True),
        )
        if updated_repos:
            typer.echo("\nAGENTS.md:")
            for repo_path in updated_repos:
                typer.echo(f"  {repo_path}: regenerated AGENTS.md")

        typer.echo(
            "\nNext: start a Claude Code session in one of the repos."
            " The MCP server will start automatically."
        )
        if with_hooks:
            typer.echo(f"\n{HOOKS_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Cursor ─────────────────────────────────────────────────────────────────


# Cursor reads MCP servers from `<repo>/.cursor/mcp.json` (per-project).
# User-global "Rules for AI" live in the IDE Settings UI, not a file path —
# so MCP wiring is per-project here.
# Docs: https://cursor.com/docs


def _configure_cursor_for_repo(repo_path: Path, *, remove: bool) -> str:
    """Add or remove the Nauro MCP entry in this repo's ``.cursor/mcp.json``."""
    return _configure_json_mcp(
        repo_path,
        config_rel_path=".cursor/mcp.json",
        label=".cursor/mcp.json",
        remove=remove,
    )


@setup_app.command(name="cursor")
def cursor(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
) -> None:
    """Configure Cursor to use Nauro for this project's repos."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro (Cursor) for project '{project_name}':\n")
    for repo_str in entry["repo_paths"]:
        repo_path = Path(repo_str)
        if not repo_path.is_dir():
            typer.echo(f"  {repo_path}: repo path missing, skipped")
            continue
        typer.echo(_configure_cursor_for_repo(repo_path, remove=remove))

    if not remove:
        typer.echo("\nNext: open this repo in Cursor and start a chat — Nauro MCP will connect.")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Codex CLI ──────────────────────────────────────────────────────────────


# Codex reads MCP servers from `~/.codex/config.toml` under `[mcp_servers.<name>]`.
# This is the user-global Codex CLI config and is shared with the IDE extension.
# Docs: https://developers.openai.com/codex/mcp


def _default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


@dataclass(frozen=True)
class _CodexNauroEntry:
    """Typed view of the ``[mcp_servers.nauro]`` keys Nauro owns.

    Only ``command`` and ``args`` belong to Nauro; any other keys the user
    added to the entry (timeouts, env, ...) are never touched. A field is
    None when the underlying key is missing or off-shape, so each owned key
    is compared and rewritten independently: a key whose value already
    matches stays untouched, preserving its formatting and comments.
    """

    command: str | None
    args: list[str] | None


def _parse_codex_nauro_entry(entry: object) -> _CodexNauroEntry:
    """Parse an existing ``mcp_servers.nauro`` value into the typed view."""
    if not isinstance(entry, dict):
        return _CodexNauroEntry(command=None, args=None)
    command = entry.get("command")
    args = entry.get("args")
    parsed_args: list[str] | None = None
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        parsed_args = [str(item) for item in args]
    return _CodexNauroEntry(
        command=str(command) if isinstance(command, str) else None,
        args=parsed_args,
    )


def _configure_codex(
    *,
    remove: bool,
    config_path: Path | None = None,
    clear_user_scope: bool = True,
) -> str:
    """Add or remove the Nauro MCP entry in ``~/.codex/config.toml``.

    The file is hand-maintained user config, so edits go through tomlkit:
    comments, formatting, and user-added keys inside the nauro entry survive,
    and only the ``command``/``args`` keys Nauro owns are rewritten. A
    config.toml that is itself a symlink is refused (a dotfile manager may
    own the real file); a symlinked parent directory works. Writes are
    atomic, preserve permission bits, and are skipped when nothing changes.

    ``clear_user_scope`` gates the remove path: when False, the codex MCP
    entry is preserved because other registered nauro projects still depend
    on it. Defaults to True so direct unit callers and the add path retain
    their previous behavior.
    """
    config_path = config_path or _default_codex_config_path()

    if remove and not clear_user_scope:
        return (
            f"Codex: preserved nauro entry in {config_path} (other nauro projects still registered)"
        )

    refusal = find_file_symlink(config_path)
    if refusal is not None:
        return f"Codex: {refusal.message}"

    original: bytes | None
    if config_path.exists():
        original = config_path.read_bytes()
        try:
            document = tomlkit.parse(original.decode("utf-8"))
        except UnicodeDecodeError:
            return f"Codex: could not parse {config_path} - not valid UTF-8"
        except TOMLParseError as exc:
            return f"Codex: could not parse {config_path} - {exc}"
    elif remove:
        return f"Codex: no nauro entry to remove in {config_path}"
    else:
        original = None
        document = tomlkit.document()

    servers = document.get("mcp_servers")
    # A hand-edited config.toml could define mcp_servers as a non-table (e.g. a
    # string); mutating it would raise. Skip with a clear message, not a crash.
    if servers is not None and not isinstance(servers, dict):
        if remove:
            return f"Codex: no nauro entry to remove in {config_path}"
        return f"Codex: mcp_servers in {config_path} is not a table, skipped"

    if remove:
        if servers is None or "nauro" not in servers:
            return f"Codex: no nauro entry to remove in {config_path}"
        # The emptied parent table is deliberately left in place: popping it
        # would rewrite user formatting beyond the entry being removed.
        del servers["nauro"]
        status = f"Codex: removed nauro from {config_path}"
    else:
        desired = _CodexNauroEntry(command=_find_nauro_command(), args=["serve", "--stdio"])
        if servers is None:
            servers = tomlkit.table(is_super_table=False)
            document["mcp_servers"] = servers
        entry = servers.get("nauro")
        current = _parse_codex_nauro_entry(entry)
        if current == desired:
            return f"Codex: nauro already configured in {config_path}"
        if isinstance(entry, dict):
            # Per-key update: a key whose value already matches keeps its
            # formatting and comments (e.g. a multiline args array).
            if current.command != desired.command:
                entry["command"] = desired.command
            if current.args != desired.args:
                entry["args"] = desired.args
        elif isinstance(servers, InlineTable):
            # A block table nested inside an inline parent renders invalid
            # TOML; match the user's inline style instead.
            inline = tomlkit.inline_table()
            inline["command"] = desired.command
            inline["args"] = desired.args
            servers["nauro"] = inline
        else:
            table = tomlkit.table()
            table["command"] = desired.command
            table["args"] = desired.args
            servers["nauro"] = table
            # Appended without a leading blank line: a reparse attributes the
            # separator to the preceding entry, which would strand a stray
            # blank line once a later remove deletes the block.
            table.trivia.indent = ""
        status = f"Codex: wrote nauro to {config_path}"

    rendered = tomlkit.dumps(document)
    if original is not None and rendered.encode("utf-8") == original:
        return status
    # newline="\n" is load-bearing: tomlkit output carries the file's original
    # line endings as literal characters, so translation would corrupt CRLF.
    atomic_write_text(config_path, rendered, newline="\n")
    return status


def _nearest_codex_hooks_repo(start: Path) -> Path | None:
    resolved = start.resolve()
    home = Path.home().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate == home:
            break
        if (candidate / ".codex" / "hooks.json").is_file():
            return candidate
    return None


@setup_app.command(name="codex")
def codex(
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's SessionStart and SubagentStart hooks into each repo's "
            "project-scope .codex/hooks.json. Codex requires review through /hooks. "
            "Removal cleans up existing hooks without this flag."
        ),
    ),
) -> None:
    """Configure Codex CLI to use Nauro (writes '~/.codex/config.toml')."""
    hook_repos: list[Path] = []
    if with_hooks and not remove:
        project_name, store_path = resolve_target_project(None)
        entry = _resolve_project_entry(project_name, store_path.name)
        hook_repos = [Path(repo_path) for repo_path in entry["repo_paths"]]

    # Standalone codex wiring is user-global and shared by every registered
    # project, so this teardown preserves the entry while any project remains
    # in the registry (it clears only on an empty registry). Clearing on the
    # last project goes through 'nauro setup all --remove'.
    registered_count = len(_registered_project_keys()) if remove else 0
    if registered_count:
        config_path = _default_codex_config_path()
        count_phrase = (
            "1 nauro project" if registered_count == 1 else f"{registered_count} nauro projects"
        )
        typer.echo(
            f"Codex: preserved nauro entry in {config_path} ({count_phrase} registered; "
            "run 'nauro setup all --remove' on the last project to clear this "
            "user-global entry)"
        )
    else:
        typer.echo(_configure_codex(remove=remove))

    hook_cleanup_unresolved = False
    if remove:
        try:
            resolution = resolve_from_cwd(Path.cwd())
            hook_repos = (
                [Path(repo_path) for repo_path in get_repo_paths(resolution.project_id)]
                if resolution is not None
                else []
            )
            nearest_hooks_repo = _nearest_codex_hooks_repo(Path.cwd())
            if nearest_hooks_repo is not None and nearest_hooks_repo not in hook_repos:
                hook_repos.append(nearest_hooks_repo)
            hook_cleanup_unresolved = not hook_repos
        except Exception:
            hook_cleanup_unresolved = True

    if with_hooks or remove:
        typer.echo("\nHooks:")
        if hook_cleanup_unresolved:
            typer.echo(
                "  Project-scoped Codex hooks were not removed because no Nauro "
                "project resolves from this directory. Run this command from each "
                "wired repo to remove them."
            )
        for repo_path in hook_repos:
            if not repo_path.is_dir():
                typer.echo(f"  {repo_path}: repo path missing, skipped")
                continue
            try:
                typer.echo(materialize_hooks_codex(repo_path, remove=remove))
            except Exception as exc:
                typer.echo(f"  {repo_path}: Codex hook wiring error - {exc}")

    if not remove:
        typer.echo("\nNext: run a Codex session — it reads ~/.codex/config.toml on start.")
        if with_hooks:
            typer.echo(f"\n{CODEX_HOOKS_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")


# ─── Hook materialization ─────────────────────────────────────────────────────


# Claude Code reads hooks from project-scope ``<repo>/.claude/settings.json``.
# The advisory UserPromptSubmit hook runs ``nauro hook user-prompt-submit`` on
# each turn; it surfaces related decisions as context and never blocks a turn.
# The MVP hook is BM25-floor only — it does not set ``NAURO_EMBEDDINGS`` — so the
# install incurs no embedding model load. The hook still resolves the embeddings
# flag internally, so the follow-up that re-admits cosine-gated embedding hits
# can flip the backend on without changing the installed command.
#
HOOK_EVENT_NAME = "UserPromptSubmit"
# The subcommand the hook entry runs; the full command is built at install time
# by prefixing the resolved absolute nauro path (see _nauro_hook_entry), so the
# hook fires even when nauro is not on the agent's launch PATH.
HOOK_SUBCOMMAND = "hook user-prompt-submit"
HOOK_TIMEOUT_SECONDS = 10

# Substring that identifies a nauro-authored hook entry on the remove path, so a
# user's own UserPromptSubmit hooks are preserved. Matches the subcommand rather
# than "nauro " so it holds regardless of how the entrypoint resolves — a bare
# "nauro", an absolute POSIX path, or a Windows "nauro.exe".
_HOOK_COMMAND_MARKER = HOOK_SUBCOMMAND


def _claude_settings_path(repo: Path) -> Path:
    return repo / ".claude" / "settings.json"


def materialize_hooks_claude_code(repo: Path, *, remove: bool) -> str:
    """Add or remove the Nauro advisory hook in ``<repo>/.claude/settings.json``.

    Add path: idempotently append the hook entry to
    ``hooks.UserPromptSubmit[].hooks[]`` only when no nauro-authored entry is
    already present. Remove path: strip only the nauro-authored entry (matched on
    the command containing ``nauro hook``), preserving any user-authored hooks
    and the surrounding structure.

    Returns a one-line status string (indented for ``setup_all_surfaces``).
    """
    refusal = find_symlink(repo, ".claude/settings.json")
    if refusal is not None:
        return f"  {repo}: {refusal.message}"
    settings_path = _claude_settings_path(repo)

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"  {repo}: could not parse .claude/settings.json - {exc}"
        if not isinstance(settings, dict):
            return f"  {repo}: .claude/settings.json is not a JSON object, skipped"
    else:
        settings = {}

    if remove:
        return _remove_hook_entry(settings_path, settings, repo)
    return _add_hook_entry(settings_path, settings, repo)


def _nauro_hook_entry() -> dict:
    return {
        "type": "command",
        "command": f"{_find_nauro_command()} {HOOK_SUBCOMMAND}",
        "timeout": HOOK_TIMEOUT_SECONDS,
    }


def _is_nauro_hook(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("command"), str)
        and _HOOK_COMMAND_MARKER in entry["command"]
    )


def _add_hook_entry(settings_path: Path, settings: dict, repo: Path) -> str:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return f"  {repo}: hooks key is not a JSON object, skipped"
    event_matchers = hooks.setdefault(HOOK_EVENT_NAME, [])
    if not isinstance(event_matchers, list):
        return f"  {repo}: hooks.{HOOK_EVENT_NAME} is not a JSON array, skipped"

    # Idempotent: if any matcher already carries a nauro hook, do nothing.
    for matcher in event_matchers:
        if isinstance(matcher, dict):
            for entry in matcher.get("hooks", []):
                if _is_nauro_hook(entry):
                    return f"  {repo}: nauro hook already present in .claude/settings.json"

    event_matchers.append({"hooks": [_nauro_hook_entry()]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    lines = [f"  {repo}: wrote nauro hook to .claude/settings.json"]
    lines.extend(public_surface_git_warnings(repo, ".claude/settings.json"))
    return "\n".join(lines)


def _remove_hook_entry(settings_path: Path, settings: dict, repo: Path) -> str:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return f"  {repo}: no nauro hook to remove"
    event_matchers = hooks.get(HOOK_EVENT_NAME)
    if not isinstance(event_matchers, list):
        return f"  {repo}: no nauro hook to remove"

    removed = False
    surviving_matchers = []
    for matcher in event_matchers:
        if not isinstance(matcher, dict):
            surviving_matchers.append(matcher)
            continue
        entries = matcher.get("hooks", [])
        kept = [e for e in entries if not _is_nauro_hook(e)]
        removed_here = len(entries) - len(kept)
        if removed_here:
            removed = True
        if removed_here == 0:
            surviving_matchers.append(matcher)
        elif kept:
            matcher = {**matcher, "hooks": kept}
            surviving_matchers.append(matcher)
        elif set(matcher) - {"hooks"}:
            surviving_matchers.append({**matcher, "hooks": []})
        # Drop only the installer-owned matcher shell with no user metadata.

    if not removed:
        return f"  {repo}: no nauro hook to remove"

    if surviving_matchers:
        hooks[HOOK_EVENT_NAME] = surviving_matchers
    else:
        hooks.pop(HOOK_EVENT_NAME, None)
    if not hooks:
        settings.pop("hooks", None)

    if settings:
        atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    else:
        settings_path.unlink()
    return f"  {repo}: removed nauro hook from .claude/settings.json"


def _codex_hooks_path(repo: Path) -> Path:
    return repo / ".codex" / "hooks.json"


@functools.cache
def _find_nauro_codex_hook_command() -> str | None:
    command = _find_nauro_command()
    if cli_utils.probe_nauro_command(command, args=_CODEX_HOOK_PROBE_ARGS):
        return command

    sibling = _interpreter_sibling_candidate()
    if (
        sibling is not None
        and sibling != command
        and cli_utils.probe_nauro_command(sibling, args=_CODEX_HOOK_PROBE_ARGS)
    ):
        typer.echo(
            f"WARNING: '{command}' does not support Codex bootstrap hooks. "
            f"Recording the current Nauro install at '{sibling}' instead. "
            "Update the durable Nauro install and re-run 'nauro setup all --with-hooks'.",
            err=True,
        )
        return sibling

    typer.echo(
        "WARNING: no installed Nauro command supports Codex bootstrap hooks. "
        "Codex hook wiring was skipped; update Nauro and re-run "
        "'nauro setup all --with-hooks'.",
        err=True,
    )
    return None


def materialize_hooks_codex(repo: Path, *, remove: bool) -> str:
    """Add or remove project-scoped Codex lifecycle hooks for ``repo``."""
    refusal = find_symlink(repo, ".codex/hooks.json")
    if refusal is not None:
        return f"  {repo}: {refusal.message}"
    hooks_path = _codex_hooks_path(repo)
    existing_text: str | None = None
    if hooks_path.exists():
        try:
            existing_text = hooks_path.read_text(encoding="utf-8")
            config = _parse_codex_hooks(existing_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"  {repo}: could not parse .codex/hooks.json - {exc}"
        except _CodexHookConfigError as exc:
            return f"  {repo}: {exc}"
    else:
        config = {}

    try:
        _validate_codex_hooks(config)
    except _CodexHookConfigError as exc:
        return f"  {repo}: {exc}"

    command = None if remove else _find_nauro_codex_hook_command()
    if not remove and command is None:
        return f"  {repo}: Codex hook wiring skipped; no compatible Nauro command"

    try:
        transformed = _transform_codex_hooks(config, command=command)
    except _CodexHookConfigError as exc:
        return f"  {repo}: {exc}"

    if remove:
        if transformed.removed == 0:
            return f"  {repo}: no nauro Codex hooks to remove"
        if transformed.config:
            atomic_write_text(hooks_path, _format_codex_hooks(transformed.config))
        else:
            hooks_path.unlink()
        return f"  {repo}: removed nauro hooks from .codex/hooks.json"

    rendered = _format_codex_hooks(transformed.config)
    if existing_text == rendered:
        return f"  {repo}: nauro hooks already present in .codex/hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(hooks_path, rendered)
    lines = [f"  {repo}: wrote nauro hooks to .codex/hooks.json"]
    lines.extend(public_surface_git_warnings(repo, ".codex/hooks.json"))
    return "\n".join(lines)


# ─── nauro setup all ────────────────────────────────────────────────────────


def setup_all_surfaces(
    project_repos: list[Path],
    *,
    remove: bool = False,
    current_project_key: str | None = None,
    store_path: Path | None = None,
    with_subagents: bool = False,
    force_overwrite: bool = False,
    with_skills: bool = False,
    with_hooks: bool = False,
    clear_user_scope_override: bool | None = None,
) -> list[str]:
    """Wire MCP and materialize skills across Claude Code, Cursor, Codex.

    Continues across per-handler errors so partial coverage still reports
    progress. Returns the cumulative status lines.

    ``current_project_key`` is the registry key (v2 id or v1 name) for the
    project being wired or torn down. When ``remove=True``, it is excluded
    from the "are there other projects?" check so per-project teardown only
    clears user-scope artifacts (Claude/Codex skill, ``~/.codex/config.toml``)
    when this is the last project on the machine.

    On the add path, when both ``current_project_key`` and ``store_path`` are
    supplied, AGENTS.md is regenerated once across the project's repos so every
    entry point (``setup claude-code``, ``setup all``, ``adopt``) produces the
    cross-tool context file. The MCP-less Cursor/Codex surfaces depend on this
    fallback layer the most, yet only ``setup claude-code`` used to write it.

    ``with_subagents`` opts into installing or removing the bundled
    ``nauro-*`` workflow subagents under ``~/.claude/agents/``. Off by
    default so existing flows that pre-date the subagent bundle keep
    their previous behavior. ``force_overwrite`` is only meaningful when
    ``with_subagents`` is True and ``remove`` is False — it replaces
    locally-modified bundled files instead of preserving them.

    ``with_skills`` opts into installing the bundled opt-in skills
    (``nauro-ship-task``, ``nauro-context``, ``nauro-loop``). Independent of
    ``with_subagents`` so users
    can adopt skills and subagents on separate cadences, though
    ``nauro-ship-task`` references the bundled ``@nauro-*`` subagents in
    its body — and ``nauro-loop`` dispatches that chain — so a caller that
    surfaces ``with_skills`` without ``with_subagents`` should warn the user.

    ``with_hooks`` opts into wiring the advisory Claude Code
    ``UserPromptSubmit`` hook and the Codex ``SessionStart`` and
    ``SubagentStart`` hooks into each repo's project-scope configuration. Off
    by default. A hook-wiring failure is caught and reported as a status line
    so it never aborts the rest of setup.

    ``clear_user_scope_override`` forces the shared-user-scope decision instead
    of deriving it from the registry. ``nauro adopt --remove`` passes ``False``
    when it un-adopts one repo of a multi-repo project: the default
    ``_user_scope_safe_to_clear`` check is project-granular, so it would wrongly
    clear codex/skill/agent artifacts that the project's other repos still need.
    Leave ``None`` for the default behavior.
    """
    if clear_user_scope_override is not None:
        clear_user_scope = clear_user_scope_override
    else:
        clear_user_scope = _user_scope_safe_to_clear(current_project_key) if remove else True

    lines: list[str] = []

    # Claude Code (MCP per-repo via direct `.mcp.json` write + skills global)
    for repo in project_repos:
        if not repo.is_dir():
            lines.append(f"  {repo}: repo path missing, skipped")
            continue
        try:
            lines.append(_configure_mcp(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Claude Code MCP ({repo}): error - {exc}")
    if not remove:
        try:
            pruned = _prune_redundant_user_scope_mcp()
            if pruned:
                lines.append(pruned)
        except Exception as exc:  # never let cleanup break wiring
            lines.append(f"Claude Code MCP (user-scope cleanup): error - {exc}")
    try:
        lines.extend(
            materialize_skills_claude_code(
                remove=remove,
                clear_user_scope=clear_user_scope,
                with_skills=with_skills,
            )
        )
    except Exception as exc:
        lines.append(f"Claude Code skills: error - {exc}")

    if with_subagents:
        try:
            lines.extend(
                materialize_agents(
                    "claude_code",
                    remove=remove,
                    force_overwrite=force_overwrite,
                    clear_user_scope=clear_user_scope,
                )
            )
        except Exception as exc:
            lines.append(f"Claude Code agents: error - {exc}")

    if with_hooks or remove:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                lines.append(materialize_hooks_claude_code(repo, remove=remove))
            except Exception as exc:
                lines.append(f"Claude Code hook ({repo}): error - {exc}")

    # Cursor (MCP per-repo + skills per-repo)
    for repo in project_repos:
        if not repo.is_dir():
            continue
        try:
            lines.append(_configure_cursor_for_repo(repo, remove=remove))
        except Exception as exc:
            lines.append(f"Cursor MCP ({repo}): error - {exc}")
        try:
            lines.extend(
                materialize_skills_cursor_for_repo(repo, remove=remove, with_skills=with_skills)
            )
        except Exception as exc:
            lines.append(f"Cursor skills ({repo}): error - {exc}")

    # Codex (MCP global + skills global)
    try:
        lines.append(_configure_codex(remove=remove, clear_user_scope=clear_user_scope))
    except Exception as exc:
        lines.append(f"Codex MCP: error - {exc}")
    try:
        lines.extend(
            materialize_skills_codex(
                remove=remove,
                clear_user_scope=clear_user_scope,
                with_skills=with_skills,
            )
        )
    except Exception as exc:
        lines.append(f"Codex skills: error - {exc}")

    if with_hooks or remove:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                lines.append(materialize_hooks_codex(repo, remove=remove))
            except Exception as exc:
                lines.append(f"Codex hooks ({repo}): error - {exc}")

    # Regenerate AGENTS.md once so context is fresh from the start on every
    # entry point that wires surfaces. Guarded on the add path and on having a
    # store to read from. warn_then_regen routes missing-repo, symlink-refusal,
    # and git-hygiene warnings into the status lines.
    if not remove and current_project_key is not None and store_path is not None:
        try:
            updated = warn_then_regen(current_project_key, store_path, warn=lines.append)
            for repo_path in updated:
                lines.append(f"  {repo_path}: regenerated AGENTS.md")
        except Exception as exc:
            lines.append(f"AGENTS.md regeneration: error - {exc}")

    # Mirror of the regen above: strip the generated AGENTS.md on teardown so a
    # removed integration leaves no orphaned context file. User content in a
    # ``# Manual`` section is preserved (the file is kept) by the helper.
    if remove:
        for repo in project_repos:
            if not repo.is_dir():
                continue
            try:
                removed_line = remove_generated_agents_md(repo)
                if removed_line:
                    lines.append(removed_line)
            except Exception as exc:
                lines.append(f"AGENTS.md removal ({repo}) failed: {exc}")

    return lines


SHIP_TASK_NEEDS_SUBAGENTS_NOTICE = (
    "nauro-ship-task references the bundled @nauro-* subagents (and nauro-loop "
    "dispatches that chain); pass `--with-subagents` to install them too."
)

# The bundled subagents allow the cloud tools by the fixed name
# `mcp__claude_ai_Nauro__*`. That prefix only resolves when the remote
# connector is named exactly `Nauro`, so surface the requirement whenever
# subagents are installed.
SUBAGENTS_CONNECTOR_NAME_NOTICE = (
    "Cloud users: name the remote MCP connector exactly `Nauro` so the bundled "
    "@nauro-* subagents' `mcp__claude_ai_Nauro__*` tools resolve."
)

HOOKS_NOTICE = (
    "The advisory hook surfaces related decisions as context on each turn "
    "(BM25 retrieval) and never blocks. Start a new Claude Code session in a "
    "wired repo for it to take effect."
)

CODEX_HOOKS_NOTICE = (
    "Codex skips new or changed hooks until you review and trust them. Start "
    "Codex in a wired repo, then open `/hooks` to review the project hooks."
)

# Multi-surface restart handoff. MCP config is read at session start, so an
# already-open session won't see the new wiring until it restarts. The
# single-tool `setup claude-code` prints its own equivalent line.
ALL_RESTART_NOTICE = (
    "Next: start a fresh agent session (Claude Code/Cursor) — MCP config is read at session start."
)


@setup_app.command(name="all")
def all_(
    project: str | None = typer.Option(
        None, "--project", help="Project name (default: resolve from cwd)."
    ),
    remove: bool = typer.Option(
        False, "--remove", help="Remove Nauro integration instead of adding it."
    ),
    with_subagents: bool = typer.Option(
        False,
        "--with-subagents",
        help=(
            "Install Nauro's bundled workflow subagents (@nauro-planner, "
            "@nauro-executor, @nauro-reviewer, @nauro-tech-lead) into "
            "~/.claude/agents/. Off by default to avoid overwriting "
            "customized files."
        ),
    ),
    force_overwrite: bool = typer.Option(
        False,
        "--force-overwrite",
        help=(
            "Overwrite ~/.claude/agents/nauro-*.md in place without saving a "
            ".bak, when --with-subagents is passed. By default, install "
            "refreshes a differing bundled file and stashes its prior content "
            "to <name>.md.bak."
        ),
    ),
    with_skills: bool = typer.Option(
        False,
        "--with-skills",
        help=(
            "Install Nauro's bundled opt-in skills "
            "(/nauro-ship-task, /nauro-context, /nauro-loop) alongside the "
            "always-installed /nauro-adopt skill. Independent of --with-subagents."
        ),
    ),
    with_hooks: bool = typer.Option(
        False,
        "--with-hooks",
        help=(
            "Wire Nauro's advisory Claude Code UserPromptSubmit hook and Codex "
            "SessionStart/SubagentStart hooks into each repo's project-scope "
            "configuration. Hooks surface decision context and never block."
        ),
    ),
) -> None:
    """Configure Claude Code, Cursor, and Codex CLI in one call."""
    project_name, _store_path = resolve_target_project(project)
    entry = _resolve_project_entry(project_name, _store_path.name)

    project_repos = [Path(rp) for rp in entry["repo_paths"]]
    action = "Removed" if remove else "Configured"
    typer.echo(f"{action} Nauro for project '{project_name}' across all surfaces:\n")
    for line in setup_all_surfaces(
        project_repos,
        remove=remove,
        current_project_key=_store_path.name,
        store_path=_store_path,
        with_subagents=with_subagents,
        force_overwrite=force_overwrite,
        with_skills=with_skills,
        with_hooks=with_hooks,
    ):
        typer.echo(line)

    if not remove and with_skills and not with_subagents:
        typer.echo(f"\n{SHIP_TASK_NEEDS_SUBAGENTS_NOTICE}")

    if not remove and with_subagents:
        typer.echo(f"\n{SUBAGENTS_CONNECTOR_NAME_NOTICE}")

    if not remove and with_hooks:
        typer.echo(f"\n{HOOKS_NOTICE}")
        typer.echo(f"\n{CODEX_HOOKS_NOTICE}")

    if not remove:
        typer.echo(f"\n{ALL_RESTART_NOTICE}")
        typer.echo(f"\n{CHECK_HINT_LINE}")
