"""Render typed setup outcomes back into the status lines commands echo.

Presentation lives here alone: every codec reports a typed outcome and this
module maps it to the exact lines the setup commands print. A command echoes
each returned string on its own ``typer.echo`` call, so a multi-line block
(a write plus its git-hygiene warnings) is returned as separate elements and
lands byte-identically to a single echo of the joined text.
"""

from __future__ import annotations

from nauro.cli.integrations.outcomes import (
    AgentKind,
    AgentOutcome,
    ArtifactOutcome,
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
    if isinstance(outcome, CodexConfigOutcome):
        return _render_codex_config(outcome)
    if isinstance(outcome, CodexHookOutcome):
        return _render_codex_hook(outcome)
    if isinstance(outcome, SkillOutcome):
        return _render_skill(outcome)
    if isinstance(outcome, AgentOutcome):
        return _render_agent(outcome)
    raise TypeError(f"unrenderable outcome: {outcome!r}")


def _render_json_mcp(o: JsonMcpOutcome) -> list[str]:
    match o.kind:
        case JsonMcpKind.REFUSED_SYMLINK:
            return [f"  {o.repo_path}: {o.refusal.message}"]
        case JsonMcpKind.PARSE_ERROR:
            return [f"  {o.repo_path}: could not parse {o.label} - {o.detail}"]
        case JsonMcpKind.NOT_JSON_OBJECT:
            return [f"  {o.repo_path}: {o.label} is not a JSON object, skipped"]
        case JsonMcpKind.MCPSERVERS_NOT_OBJECT:
            return [f"  {o.repo_path}: mcpServers in {o.label} is not a JSON object, skipped"]
        case JsonMcpKind.NOTHING_TO_REMOVE:
            return [f"  {o.repo_path}: no nauro entry to remove"]
        case JsonMcpKind.REMOVED:
            return [f"  {o.repo_path}: removed nauro from {o.label}"]
        case JsonMcpKind.WROTE:
            return [f"  {o.repo_path}: wrote nauro to {o.label}", *o.git_warnings]


def _render_claude_hook(o: ClaudeHookOutcome) -> list[str]:
    match o.kind:
        case ClaudeHookKind.REFUSED_SYMLINK:
            return [f"  {o.repo}: {o.refusal.message}"]
        case ClaudeHookKind.PARSE_ERROR:
            return [f"  {o.repo}: could not parse .claude/settings.json - {o.detail}"]
        case ClaudeHookKind.NOT_JSON_OBJECT:
            return [f"  {o.repo}: .claude/settings.json is not a JSON object, skipped"]
        case ClaudeHookKind.HOOKS_NOT_OBJECT:
            return [f"  {o.repo}: hooks key is not a JSON object, skipped"]
        case ClaudeHookKind.EVENT_NOT_ARRAY:
            return [f"  {o.repo}: hooks.UserPromptSubmit is not a JSON array, skipped"]
        case ClaudeHookKind.ALREADY_PRESENT:
            return [f"  {o.repo}: nauro hook already present in .claude/settings.json"]
        case ClaudeHookKind.WROTE:
            return [f"  {o.repo}: wrote nauro hook to .claude/settings.json", *o.git_warnings]
        case ClaudeHookKind.NOTHING_TO_REMOVE:
            return [f"  {o.repo}: no nauro hook to remove"]
        case ClaudeHookKind.REMOVED:
            return [f"  {o.repo}: removed nauro hook from .claude/settings.json"]


def _render_claude_user_config(o: ClaudeUserConfigOutcome) -> list[str]:
    match o.kind:
        case ClaudeUserConfigKind.REFUSED_SYMLINK:
            return [f"  skipped user-scope prune: {o.refusal.message}"]
        case ClaudeUserConfigKind.INVALID_UTF8:
            return ["  skipped user-scope prune: ~/.claude.json is not valid UTF-8"]
        case ClaudeUserConfigKind.PRUNED:
            return [
                "  removed redundant user-scope HTTP nauro entry from ~/.claude.json "
                "(project-scope stdio is canonical)"
            ]


def _render_legacy(o: LegacyOutcome) -> list[str]:
    match o.kind:
        case LegacyKind.REFUSED_SYMLINK:
            return [f"  {o.repo_path}: {o.refusal.message}"]
        case LegacyKind.REMOVED_DELETED_FILE:
            return [f"  {o.repo_path}: removed legacy Nauro block (deleted empty CLAUDE.md)"]
        case LegacyKind.REMOVED_BLOCK:
            return [f"  {o.repo_path}: removed legacy Nauro block from CLAUDE.md"]


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


def _render_codex_hook(o: CodexHookOutcome) -> list[str]:
    match o.kind:
        case CodexHookKind.REFUSED_SYMLINK:
            return [f"  {o.repo}: {o.refusal.message}"]
        case CodexHookKind.PARSE_ERROR:
            return [f"  {o.repo}: could not parse .codex/hooks.json - {o.detail}"]
        case CodexHookKind.CONFIG_ERROR:
            return [f"  {o.repo}: {o.detail}"]
        case CodexHookKind.NO_COMMAND:
            return [f"  {o.repo}: Codex hook wiring skipped; no compatible Nauro command"]
        case CodexHookKind.NOTHING_TO_REMOVE:
            return [f"  {o.repo}: no nauro Codex hooks to remove"]
        case CodexHookKind.REMOVED:
            return [f"  {o.repo}: removed nauro hooks from .codex/hooks.json"]
        case CodexHookKind.ALREADY_PRESENT:
            return [f"  {o.repo}: nauro hooks already present in .codex/hooks.json"]
        case CodexHookKind.WROTE:
            return [f"  {o.repo}: wrote nauro hooks to .codex/hooks.json", *o.git_warnings]


def _render_skill(o: SkillOutcome) -> list[str]:
    match o.kind:
        case SkillKind.REFUSED_SYMLINK:
            if o.repo is not None:
                return [f"  {o.repo}: {o.refusal.message}"]
            return [f"  {o.refusal.message}"]
        case SkillKind.PRESERVED:
            return [f"  preserved {o.base_label}/nauro-* (other nauro projects still registered)"]
        case SkillKind.WROTE:
            return [f"  wrote {o.target}"]
        case SkillKind.REMOVED:
            return [f"  removed {o.target}"]
        case SkillKind.ABSENT:
            return [f"  no skill at {o.target}"]


def _render_agent(o: AgentOutcome) -> list[str]:
    match o.kind:
        case AgentKind.SURFACE_NOT_IMPLEMENTED:
            return [f"  skipped ~/.{o.surface} agents (not yet implemented)"]
        case AgentKind.SURFACE_INVALID:
            return [f"  skipped agents on surface {o.surface!r}: {o.detail}"]
        case AgentKind.PRESERVED:
            return ["  preserved ~/.claude/agents/nauro-* (other nauro projects still registered)"]
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
