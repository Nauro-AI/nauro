"""Render typed setup outcomes back into the status lines commands echo.

Presentation lives here alone: every codec reports a typed outcome and this
module maps it to the exact lines the setup commands print. A command echoes
each returned string on its own ``typer.echo`` call, so a multi-line block
(a write plus its git-hygiene warnings) is returned as separate elements and
lands byte-identically to a single echo of the joined text.
"""

from __future__ import annotations

from nauro.cli.git_hygiene import GitIgnoreKind, GitIgnoreResult
from nauro.cli.integrations.outcomes import (
    AgentKind,
    AgentOutcome,
    ArtifactOutcome,
    BridgeKind,
    BridgeOutcome,
    ClaudeHookKind,
    ClaudeHookOutcome,
    ClaudeUserConfigKind,
    ClaudeUserConfigOutcome,
    CodexConfigKind,
    CodexConfigOutcome,
    CodexHookKind,
    CodexHookOutcome,
    HandlerErrorOutcome,
    JsonMcpKind,
    JsonMcpOutcome,
    LegacyKind,
    LegacyOutcome,
    RawLine,
    SkillKind,
    SkillOutcome,
)


def render(outcome: ArtifactOutcome) -> list[str]:
    """Flatten one outcome into the status lines a command echoes."""
    if isinstance(outcome, RawLine):
        return [outcome.text]
    if isinstance(outcome, HandlerErrorOutcome):
        return [outcome.message]
    if isinstance(outcome, JsonMcpOutcome):
        return _render_json_mcp(outcome)
    if isinstance(outcome, ClaudeHookOutcome):
        return _render_claude_hook(outcome)
    if isinstance(outcome, ClaudeUserConfigOutcome):
        return _render_claude_user_config(outcome)
    if isinstance(outcome, LegacyOutcome):
        return _render_legacy(outcome)
    if isinstance(outcome, BridgeOutcome):
        return _render_bridge(outcome)
    if isinstance(outcome, CodexConfigOutcome):
        return _render_codex_config(outcome)
    if isinstance(outcome, CodexHookOutcome):
        return _render_codex_hook(outcome)
    if isinstance(outcome, SkillOutcome):
        return _render_skill(outcome)
    if isinstance(outcome, AgentOutcome):
        return _render_agent(outcome)
    raise TypeError(f"unrenderable outcome: {outcome!r}")


def _tracked_refusal_lines(prefix: str, rel_path: str, what: str) -> list[str]:
    """Shared wording for refusing to write machine-local wiring into a tracked file."""
    return [
        f"{prefix}: {rel_path} is tracked by git - skipped writing {what}",
        (
            f"    It records absolute paths that only work on this machine. "
            f"Run `git rm --cached {rel_path}`, commit, and re-run; nauro will "
            f"then git-ignore it so each machine keeps its own copy."
        ),
    ]


def _render_gitignore(result: GitIgnoreResult | None) -> list[str]:
    """Status lines for a managed .gitignore update carried by a codec outcome."""
    if result is None:
        return []
    match result.kind:
        case GitIgnoreKind.ADDED:
            return [
                f"    added {result.rel_path} to .gitignore "
                "(machine-local wiring; commit this change)"
            ]
        case GitIgnoreKind.REMOVED_ENTRY | GitIgnoreKind.REMOVED_BLOCK:
            return [f"    removed {result.rel_path} from .gitignore"]
        case GitIgnoreKind.REFUSED_SYMLINK:
            return [f"    Warning: did not update .gitignore: {result.refusal.message}"]
        case GitIgnoreKind.REFUSED_UNREADABLE:
            return ["    Warning: did not update .gitignore: not valid UTF-8"]
        case GitIgnoreKind.REFUSED_MALFORMED_BLOCK:
            return [
                "    Warning: did not update .gitignore: its nauro-managed "
                "block markers are malformed; repair or remove them and re-run"
            ]
        case GitIgnoreKind.REFUSED_UNWRITABLE:
            return [f"    Warning: could not write .gitignore: {result.detail}"]
        case (
            GitIgnoreKind.ALREADY_COVERED
            | GitIgnoreKind.SKIPPED_NON_GIT
            | GitIgnoreKind.NOTHING_TO_REMOVE
        ):
            return []
        case _:
            raise TypeError(f"unrenderable GitIgnoreResult kind: {result.kind!r}")


def _render_json_mcp(o: JsonMcpOutcome) -> list[str]:
    match o.kind:
        case JsonMcpKind.REFUSED_SYMLINK:
            return [f"  {o.repo_path}: {o.refusal.message}"]
        case JsonMcpKind.REFUSED_TRACKED:
            return _tracked_refusal_lines(f"  {o.repo_path}", o.label, "machine-local MCP wiring")
        case JsonMcpKind.PARSE_ERROR:
            return [f"  {o.repo_path}: could not parse {o.label} - {o.detail}"]
        case JsonMcpKind.NOT_JSON_OBJECT:
            return [f"  {o.repo_path}: {o.label} is not a JSON object, skipped"]
        case JsonMcpKind.MCPSERVERS_NOT_OBJECT:
            return [f"  {o.repo_path}: mcpServers in {o.label} is not a JSON object, skipped"]
        case JsonMcpKind.NOTHING_TO_REMOVE:
            return [f"  {o.repo_path}: no nauro entry to remove", *_render_gitignore(o.gitignore)]
        case JsonMcpKind.REMOVED:
            return [
                f"  {o.repo_path}: removed nauro from {o.label}",
                *_render_gitignore(o.gitignore),
            ]
        case JsonMcpKind.WROTE:
            return [
                f"  {o.repo_path}: wrote nauro to {o.label}",
                *_render_gitignore(o.gitignore),
                *o.git_warnings,
            ]
        case _:
            raise TypeError(f"unrenderable JsonMcpOutcome kind: {o.kind!r}")


def _render_claude_hook(o: ClaudeHookOutcome) -> list[str]:
    legacy_cleanup_add = (
        [
            "    moved stale nauro hook out of .claude/settings.json "
            "(machine-local wiring lives in .claude/settings.local.json; "
            "commit the cleanup)"
        ]
        if o.legacy_cleaned
        else []
    )
    match o.kind:
        case ClaudeHookKind.REFUSED_SYMLINK:
            return [f"  {o.repo}: {o.refusal.message}"]
        case ClaudeHookKind.REFUSED_TRACKED:
            return _tracked_refusal_lines(
                f"  {o.repo}", ".claude/settings.local.json", "machine-local hook wiring"
            )
        case ClaudeHookKind.PARSE_ERROR:
            return [f"  {o.repo}: could not parse .claude/settings.local.json - {o.detail}"]
        case ClaudeHookKind.NOT_JSON_OBJECT:
            return [f"  {o.repo}: .claude/settings.local.json is not a JSON object, skipped"]
        case ClaudeHookKind.HOOKS_NOT_OBJECT:
            return [f"  {o.repo}: hooks key is not a JSON object, skipped"]
        case ClaudeHookKind.EVENT_NOT_ARRAY:
            return [f"  {o.repo}: hooks.UserPromptSubmit is not a JSON array, skipped"]
        case ClaudeHookKind.ALREADY_PRESENT:
            return [
                f"  {o.repo}: nauro hook already present in .claude/settings.local.json",
                *_render_gitignore(o.gitignore),
                *legacy_cleanup_add,
            ]
        case ClaudeHookKind.WROTE:
            return [
                f"  {o.repo}: wrote nauro hook to .claude/settings.local.json",
                *_render_gitignore(o.gitignore),
                *legacy_cleanup_add,
                *o.git_warnings,
            ]
        case ClaudeHookKind.NOTHING_TO_REMOVE:
            return [f"  {o.repo}: no nauro hook to remove", *_render_gitignore(o.gitignore)]
        case ClaudeHookKind.REMOVED:
            removed_from = (
                ".claude/settings.local.json and .claude/settings.json"
                if o.legacy_cleaned
                else ".claude/settings.local.json"
            )
            return [
                f"  {o.repo}: removed nauro hook from {removed_from}",
                *_render_gitignore(o.gitignore),
            ]
        case _:
            raise TypeError(f"unrenderable ClaudeHookOutcome kind: {o.kind!r}")


def _render_claude_user_config(o: ClaudeUserConfigOutcome) -> list[str]:
    match o.kind:
        case ClaudeUserConfigKind.REFUSED_SYMLINK:
            return [f"  skipped user-scope prune: {o.refusal.message}"]
        case ClaudeUserConfigKind.INVALID_UTF8:
            return ["  skipped user-scope prune: ~/.claude.json is not valid UTF-8"]
        case ClaudeUserConfigKind.NOT_JSON_OBJECT:
            return ["  skipped user-scope prune: ~/.claude.json is not a JSON object"]
        case ClaudeUserConfigKind.PRUNED:
            return [
                "  removed redundant user-scope HTTP nauro entry from ~/.claude.json "
                "(project-scope stdio is canonical)"
            ]
        case _:
            raise TypeError(f"unrenderable ClaudeUserConfigOutcome kind: {o.kind!r}")


def _render_legacy(o: LegacyOutcome) -> list[str]:
    match o.kind:
        case LegacyKind.REFUSED_SYMLINK:
            return [f"  {o.repo_path}: {o.refusal.message}"]
        case LegacyKind.REMOVED_DELETED_FILE:
            return [f"  {o.repo_path}: removed legacy Nauro block (deleted empty CLAUDE.md)"]
        case LegacyKind.REMOVED_BLOCK:
            return [f"  {o.repo_path}: removed legacy Nauro block from CLAUDE.md"]
        case _:
            raise TypeError(f"unrenderable LegacyOutcome kind: {o.kind!r}")


def _render_bridge(o: BridgeOutcome) -> list[str]:
    match o.kind:
        case BridgeKind.WROTE | BridgeKind.KEPT:
            # WROTE and KEPT share one state line: reruns stay byte-stable,
            # matching the .mcp.json/AGENTS.md sinks that re-report identically.
            return [f"  {o.repo_path}: CLAUDE.md imports AGENTS.md (Claude Code bridge)"]
        case BridgeKind.FOREIGN_PRESENT:
            return []
        case BridgeKind.ADVISORY:
            return [
                f"  {o.repo_path}: CLAUDE.md exists without an @AGENTS.md import; "
                "add '@AGENTS.md' so Claude Code loads Nauro's shared context"
            ]
        case BridgeKind.REFUSED_SYMLINK:
            return [f"  {o.repo_path}: {o.refusal.message}"]
        case BridgeKind.REMOVED:
            return [f"  {o.repo_path}: removed CLAUDE.md bridge"]
        case BridgeKind.STRIPPED:
            return [f"  {o.repo_path}: removed CLAUDE.md bridge import, kept your content"]
        case BridgeKind.NOTHING_TO_REMOVE:
            return []
        case BridgeKind.FAILED:
            return [f"  {o.repo_path}: CLAUDE.md bridge error - {o.detail}"]
        case _:
            raise TypeError(f"unrenderable BridgeOutcome kind: {o.kind!r}")


def _render_codex_config(o: CodexConfigOutcome) -> list[str]:
    match o.kind:
        case CodexConfigKind.PRESERVED_OTHER_PROJECTS:
            return [
                f"Codex: preserved nauro entry in {o.config_path} "
                "(other nauro projects still registered)"
            ]
        case CodexConfigKind.REFUSED_SYMLINK:
            return [f"Codex: {o.refusal.message}"]
        case CodexConfigKind.PARSE_ERROR_UTF8:
            return [f"Codex: could not parse {o.config_path} - not valid UTF-8"]
        case CodexConfigKind.PARSE_ERROR_TOML:
            return [f"Codex: could not parse {o.config_path} - {o.detail}"]
        case CodexConfigKind.NOTHING_TO_REMOVE:
            return [f"Codex: no nauro entry to remove in {o.config_path}"]
        case CodexConfigKind.MCPSERVERS_NOT_TABLE:
            return [f"Codex: mcp_servers in {o.config_path} is not a table, skipped"]
        case CodexConfigKind.REMOVED:
            return [f"Codex: removed nauro from {o.config_path}"]
        case CodexConfigKind.ALREADY_CONFIGURED:
            return [f"Codex: nauro already configured in {o.config_path}"]
        case CodexConfigKind.WROTE:
            return [f"Codex: wrote nauro to {o.config_path}"]
        case _:
            raise TypeError(f"unrenderable CodexConfigOutcome kind: {o.kind!r}")


def _render_codex_hook(o: CodexHookOutcome) -> list[str]:
    match o.kind:
        case CodexHookKind.REFUSED_SYMLINK:
            return [f"  {o.repo}: {o.refusal.message}"]
        case CodexHookKind.REFUSED_TRACKED:
            return _tracked_refusal_lines(
                f"  {o.repo}", ".codex/hooks.json", "machine-local hook wiring"
            )
        case CodexHookKind.PARSE_ERROR:
            return [f"  {o.repo}: could not parse .codex/hooks.json - {o.detail}"]
        case CodexHookKind.CONFIG_ERROR:
            return [f"  {o.repo}: {o.detail}"]
        case CodexHookKind.NO_COMMAND:
            return [f"  {o.repo}: Codex hook wiring skipped; no compatible Nauro command"]
        case CodexHookKind.NOTHING_TO_REMOVE:
            return [
                f"  {o.repo}: no nauro Codex hooks to remove",
                *_render_gitignore(o.gitignore),
            ]
        case CodexHookKind.REMOVED:
            return [
                f"  {o.repo}: removed nauro hooks from .codex/hooks.json",
                *_render_gitignore(o.gitignore),
            ]
        case CodexHookKind.ALREADY_PRESENT:
            return [
                f"  {o.repo}: nauro hooks already present in .codex/hooks.json",
                *_render_gitignore(o.gitignore),
            ]
        case CodexHookKind.WROTE:
            return [
                f"  {o.repo}: wrote nauro hooks to .codex/hooks.json",
                *_render_gitignore(o.gitignore),
                *o.git_warnings,
            ]
        case _:
            raise TypeError(f"unrenderable CodexHookOutcome kind: {o.kind!r}")


def _render_skill(o: SkillOutcome) -> list[str]:
    match o.kind:
        case SkillKind.REFUSED_SYMLINK:
            if o.repo is not None:
                return [f"  {o.repo}: {o.refusal.message}"]
            return [f"  {o.refusal.message}"]
        case SkillKind.PRESERVED:
            return [f"  preserved {o.base_label}/nauro-* (other nauro projects still registered)"]
        case SkillKind.PRESERVED_MODIFIED:
            return [f"  preserved {o.target} (locally modified)"]
        case SkillKind.WROTE:
            return [f"  wrote {o.target}"]
        case SkillKind.UNCHANGED:
            return [f"  unchanged {o.target}"]
        case SkillKind.OVERWROTE:
            return [f"  overwrote {o.target}"]
        case SkillKind.UPDATED:
            return [f"  updated {o.target} (previous saved to {o.backup_name})"]
        case SkillKind.MIGRATED_LEGACY:
            return [f"  moved legacy skill {o.source} to {o.backup_path}"]
        case SkillKind.REMOVED:
            return [f"  removed {o.target}"]
        case SkillKind.ABSENT:
            return [f"  no skill at {o.target}"]
        case _:
            raise TypeError(f"unrenderable SkillOutcome kind: {o.kind!r}")


def _render_agent(o: AgentOutcome) -> list[str]:
    match o.kind:
        case AgentKind.SURFACE_NOT_IMPLEMENTED:
            return [f"  skipped ~/.{o.surface} agents (not yet implemented)"]
        case AgentKind.SURFACE_INVALID:
            return [f"  skipped agents on surface {o.surface!r}: {o.detail}"]
        case AgentKind.PRESERVED:
            base = "~/.codex/agents" if o.surface == "codex" else "~/.claude/agents"
            return [f"  preserved {base}/nauro-* (other nauro projects still registered)"]
        case AgentKind.REFUSED_SYMLINK:
            return [f"  {o.refusal.message}"]
        case AgentKind.UNCHANGED:
            return [f"  unchanged {o.target}"]
        case AgentKind.OVERWROTE:
            return [f"  overwrote {o.target}"]
        case AgentKind.UPDATED:
            return [f"  updated {o.target} (previous saved to {o.backup_name})"]
        case AgentKind.INSTALLED:
            return [f"  installed {o.target}"]
        case AgentKind.ABSENT:
            return [f"  no agent at {o.target}"]
        case AgentKind.REMOVED:
            return [f"  removed {o.target}"]
        case AgentKind.PRESERVED_MODIFIED:
            return [f"  preserved {o.target} (locally modified)"]
        case _:
            raise TypeError(f"unrenderable AgentOutcome kind: {o.kind!r}")
