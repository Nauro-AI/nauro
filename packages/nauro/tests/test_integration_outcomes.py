"""Typed-outcome pipeline: RawLine carrier, render dispatch, and a wording table.

The table below pins ``render()``'s exact string output for every outcome kind
across every codec — the happy-path WROTE/REMOVED lines and each error/skip
branch. It is the guard that makes a future wording edit fail a test rather than
silently changing what ``nauro setup`` prints.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nauro.cli.git_hygiene import GitIgnoreKind, GitIgnoreResult
from nauro.cli.integrations.outcomes import (
    AgentKind,
    AgentOutcome,
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
from nauro.cli.integrations.render import render
from nauro.store.write_safety import SymlinkRefusal, UserSymlinkRefusal

REPO = Path("/repo")
CFG = Path("/home/.codex/config.toml")
TARGET = Path("/t/nauro-adopt/SKILL.md")
REPO_REFUSAL = SymlinkRefusal(REPO / ".mcp.json", REPO / ".mcp.json")
USER_REFUSAL = UserSymlinkRefusal(Path("/home/.claude.json"))
GITIGNORE_REFUSAL = SymlinkRefusal(REPO / ".gitignore", REPO / ".gitignore")

TRACKED_MCP_LINES = [
    f"  {REPO}: .mcp.json is tracked by git - skipped writing machine-local MCP wiring",
    (
        "    It records absolute paths that only work on this machine. "
        "Run `git rm --cached .mcp.json`, commit, and re-run; nauro will "
        "then git-ignore it so each machine keeps its own copy."
    ),
]


def test_render_rawline_returns_verbatim_text():
    assert render(RawLine("x")) == ["x"]


def test_rawline_is_frozen():
    line = RawLine("x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.text = "y"


# One entry per (outcome, exact rendered lines). Covers every Kind member of
# every codec, so a wording change to any branch fails here.
RENDER_CASES = [
    (RawLine("verbatim"), ["verbatim"]),
    (HandlerErrorOutcome("handler blew up"), ["handler blew up"]),
    # ── JsonMcp ──
    (
        JsonMcpOutcome(JsonMcpKind.REFUSED_SYMLINK, REPO, ".mcp.json", refusal=REPO_REFUSAL),
        [f"  {REPO}: {REPO_REFUSAL.message}"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.PARSE_ERROR, REPO, ".mcp.json", detail="boom"),
        [f"  {REPO}: could not parse .mcp.json - boom"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.NOT_JSON_OBJECT, REPO, ".mcp.json"),
        [f"  {REPO}: .mcp.json is not a JSON object, skipped"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.MCPSERVERS_NOT_OBJECT, REPO, ".mcp.json"),
        [f"  {REPO}: mcpServers in .mcp.json is not a JSON object, skipped"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.NOTHING_TO_REMOVE, REPO, ".mcp.json"),
        [f"  {REPO}: no nauro entry to remove"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.REMOVED, REPO, ".mcp.json"),
        [f"  {REPO}: removed nauro from .mcp.json"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.WROTE, REPO, ".mcp.json"),
        [f"  {REPO}: wrote nauro to .mcp.json"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.WROTE, REPO, ".mcp.json", git_warnings=("  a git note",)),
        [f"  {REPO}: wrote nauro to .mcp.json", "  a git note"],
    ),
    (
        JsonMcpOutcome(JsonMcpKind.REFUSED_TRACKED, REPO, ".mcp.json"),
        TRACKED_MCP_LINES,
    ),
    # ── JsonMcp with managed-gitignore results attached ──
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.ADDED, ".mcp.json"),
        ),
        [
            f"  {REPO}: wrote nauro to .mcp.json",
            "    added .mcp.json to .gitignore (machine-local wiring; commit this change)",
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.ALREADY_COVERED, ".mcp.json"),
        ),
        [f"  {REPO}: wrote nauro to .mcp.json"],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.SKIPPED_NON_GIT, ".mcp.json"),
        ),
        [f"  {REPO}: wrote nauro to .mcp.json"],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(
                GitIgnoreKind.REFUSED_SYMLINK, ".mcp.json", refusal=GITIGNORE_REFUSAL
            ),
        ),
        [
            f"  {REPO}: wrote nauro to .mcp.json",
            f"    Warning: did not update .gitignore: {GITIGNORE_REFUSAL.message}",
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.REFUSED_UNREADABLE, ".mcp.json"),
        ),
        [
            f"  {REPO}: wrote nauro to .mcp.json",
            "    Warning: did not update .gitignore: not valid UTF-8",
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.REFUSED_MALFORMED_BLOCK, ".mcp.json"),
        ),
        [
            f"  {REPO}: wrote nauro to .mcp.json",
            (
                "    Warning: did not update .gitignore: its nauro-managed "
                "block markers are malformed; repair or remove them and re-run"
            ),
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.WROTE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(
                GitIgnoreKind.REFUSED_UNWRITABLE, ".mcp.json", detail="disk full"
            ),
        ),
        [
            f"  {REPO}: wrote nauro to .mcp.json",
            "    Warning: could not write .gitignore: disk full",
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.REMOVED,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.REMOVED_ENTRY, ".mcp.json"),
        ),
        [
            f"  {REPO}: removed nauro from .mcp.json",
            "    removed .mcp.json from .gitignore",
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.REMOVED,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.REMOVED_BLOCK, ".mcp.json"),
        ),
        [
            f"  {REPO}: removed nauro from .mcp.json",
            "    removed .mcp.json from .gitignore",
        ],
    ),
    (
        JsonMcpOutcome(
            JsonMcpKind.NOTHING_TO_REMOVE,
            REPO,
            ".mcp.json",
            gitignore=GitIgnoreResult(GitIgnoreKind.NOTHING_TO_REMOVE, ".mcp.json"),
        ),
        [f"  {REPO}: no nauro entry to remove"],
    ),
    # ── ClaudeHook ──
    (
        ClaudeHookOutcome(ClaudeHookKind.REFUSED_SYMLINK, REPO, refusal=REPO_REFUSAL),
        [f"  {REPO}: {REPO_REFUSAL.message}"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.PARSE_ERROR, REPO, detail="boom"),
        [f"  {REPO}: could not parse .claude/settings.local.json - boom"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.NOT_JSON_OBJECT, REPO),
        [f"  {REPO}: .claude/settings.local.json is not a JSON object, skipped"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.HOOKS_NOT_OBJECT, REPO),
        [f"  {REPO}: hooks key is not a JSON object, skipped"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.EVENT_NOT_ARRAY, REPO),
        [f"  {REPO}: hooks.UserPromptSubmit is not a JSON array, skipped"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.ALREADY_PRESENT, REPO),
        [f"  {REPO}: nauro hook already present in .claude/settings.local.json"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.ALREADY_PRESENT, REPO, legacy_cleaned=True),
        [
            f"  {REPO}: nauro hook already present in .claude/settings.local.json",
            (
                "    moved stale nauro hook out of .claude/settings.json "
                "(machine-local wiring lives in .claude/settings.local.json; "
                "commit the cleanup)"
            ),
        ],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.WROTE, REPO),
        [f"  {REPO}: wrote nauro hook to .claude/settings.local.json"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.WROTE, REPO, legacy_cleaned=True),
        [
            f"  {REPO}: wrote nauro hook to .claude/settings.local.json",
            (
                "    moved stale nauro hook out of .claude/settings.json "
                "(machine-local wiring lives in .claude/settings.local.json; "
                "commit the cleanup)"
            ),
        ],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.REFUSED_TRACKED, REPO),
        [
            f"  {REPO}: .claude/settings.local.json is tracked by git - "
            "skipped writing machine-local hook wiring",
            (
                "    It records absolute paths that only work on this machine. "
                "Run `git rm --cached .claude/settings.local.json`, commit, and "
                "re-run; nauro will then git-ignore it so each machine keeps its "
                "own copy."
            ),
        ],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.NOTHING_TO_REMOVE, REPO),
        [f"  {REPO}: no nauro hook to remove"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.REMOVED, REPO),
        [f"  {REPO}: removed nauro hook from .claude/settings.local.json"],
    ),
    (
        ClaudeHookOutcome(ClaudeHookKind.REMOVED, REPO, legacy_cleaned=True),
        [
            f"  {REPO}: removed nauro hook from .claude/settings.local.json "
            "and .claude/settings.json"
        ],
    ),
    # ── ClaudeUserConfig ──
    (
        ClaudeUserConfigOutcome(ClaudeUserConfigKind.REFUSED_SYMLINK, refusal=USER_REFUSAL),
        [f"  skipped user-scope prune: {USER_REFUSAL.message}"],
    ),
    (
        ClaudeUserConfigOutcome(ClaudeUserConfigKind.INVALID_UTF8),
        ["  skipped user-scope prune: ~/.claude.json is not valid UTF-8"],
    ),
    (
        ClaudeUserConfigOutcome(ClaudeUserConfigKind.NOT_JSON_OBJECT),
        ["  skipped user-scope prune: ~/.claude.json is not a JSON object"],
    ),
    (
        ClaudeUserConfigOutcome(ClaudeUserConfigKind.PRUNED),
        [
            "  removed redundant user-scope HTTP nauro entry from ~/.claude.json "
            "(project-scope stdio is canonical)"
        ],
    ),
    # ── Legacy ──
    (
        LegacyOutcome(LegacyKind.REFUSED_SYMLINK, REPO, refusal=REPO_REFUSAL),
        [f"  {REPO}: {REPO_REFUSAL.message}"],
    ),
    (
        LegacyOutcome(LegacyKind.REMOVED_DELETED_FILE, REPO),
        [f"  {REPO}: removed legacy Nauro block (deleted empty CLAUDE.md)"],
    ),
    (
        LegacyOutcome(LegacyKind.REMOVED_BLOCK, REPO),
        [f"  {REPO}: removed legacy Nauro block from CLAUDE.md"],
    ),
    # ── Bridge ──
    (
        BridgeOutcome(BridgeKind.WROTE, REPO),
        [f"  {REPO}: CLAUDE.md imports AGENTS.md (Claude Code bridge)"],
    ),
    (
        BridgeOutcome(BridgeKind.KEPT, REPO),
        [f"  {REPO}: CLAUDE.md imports AGENTS.md (Claude Code bridge)"],
    ),
    (BridgeOutcome(BridgeKind.FOREIGN_PRESENT, REPO), []),
    (
        BridgeOutcome(BridgeKind.ADVISORY, REPO),
        [
            f"  {REPO}: CLAUDE.md exists without an @AGENTS.md import; "
            "add '@AGENTS.md' so Claude Code loads Nauro's shared context"
        ],
    ),
    (
        BridgeOutcome(BridgeKind.REFUSED_SYMLINK, REPO, refusal=REPO_REFUSAL),
        [f"  {REPO}: {REPO_REFUSAL.message}"],
    ),
    (BridgeOutcome(BridgeKind.REMOVED, REPO), [f"  {REPO}: removed CLAUDE.md bridge"]),
    (
        BridgeOutcome(BridgeKind.STRIPPED, REPO),
        [f"  {REPO}: removed CLAUDE.md bridge import, kept your content"],
    ),
    (BridgeOutcome(BridgeKind.NOTHING_TO_REMOVE, REPO), []),
    (
        BridgeOutcome(BridgeKind.FAILED, REPO, detail="CLAUDE.md is not a regular file"),
        [f"  {REPO}: CLAUDE.md bridge error - CLAUDE.md is not a regular file"],
    ),
    # ── CodexConfig ──
    (
        CodexConfigOutcome(CodexConfigKind.PRESERVED_OTHER_PROJECTS, CFG),
        [f"Codex: preserved nauro entry in {CFG} (other nauro projects still registered)"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.REFUSED_SYMLINK, CFG, refusal=USER_REFUSAL),
        [f"Codex: {USER_REFUSAL.message}"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.PARSE_ERROR_UTF8, CFG),
        [f"Codex: could not parse {CFG} - not valid UTF-8"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.PARSE_ERROR_TOML, CFG, detail="boom"),
        [f"Codex: could not parse {CFG} - boom"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.NOTHING_TO_REMOVE, CFG),
        [f"Codex: no nauro entry to remove in {CFG}"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.MCPSERVERS_NOT_TABLE, CFG),
        [f"Codex: mcp_servers in {CFG} is not a table, skipped"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.REMOVED, CFG),
        [f"Codex: removed nauro from {CFG}"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.ALREADY_CONFIGURED, CFG),
        [f"Codex: nauro already configured in {CFG}"],
    ),
    (
        CodexConfigOutcome(CodexConfigKind.WROTE, CFG),
        [f"Codex: wrote nauro to {CFG}"],
    ),
    # ── CodexHook ──
    (
        CodexHookOutcome(CodexHookKind.REFUSED_SYMLINK, REPO, refusal=REPO_REFUSAL),
        [f"  {REPO}: {REPO_REFUSAL.message}"],
    ),
    (
        CodexHookOutcome(CodexHookKind.PARSE_ERROR, REPO, detail="boom"),
        [f"  {REPO}: could not parse .codex/hooks.json - boom"],
    ),
    (
        CodexHookOutcome(CodexHookKind.CONFIG_ERROR, REPO, detail="bad config"),
        [f"  {REPO}: bad config"],
    ),
    (
        CodexHookOutcome(CodexHookKind.NO_COMMAND, REPO),
        [f"  {REPO}: Codex hook wiring skipped; no compatible Nauro command"],
    ),
    (
        CodexHookOutcome(CodexHookKind.NOTHING_TO_REMOVE, REPO),
        [f"  {REPO}: no nauro Codex hooks to remove"],
    ),
    (
        CodexHookOutcome(CodexHookKind.REMOVED, REPO),
        [f"  {REPO}: removed nauro hooks from .codex/hooks.json"],
    ),
    (
        CodexHookOutcome(CodexHookKind.ALREADY_PRESENT, REPO),
        [f"  {REPO}: nauro hooks already present in .codex/hooks.json"],
    ),
    (
        CodexHookOutcome(CodexHookKind.WROTE, REPO),
        [f"  {REPO}: wrote nauro hooks to .codex/hooks.json"],
    ),
    (
        CodexHookOutcome(CodexHookKind.REFUSED_TRACKED, REPO),
        [
            f"  {REPO}: .codex/hooks.json is tracked by git - "
            "skipped writing machine-local hook wiring",
            (
                "    It records absolute paths that only work on this machine. "
                "Run `git rm --cached .codex/hooks.json`, commit, and re-run; "
                "nauro will then git-ignore it so each machine keeps its own copy."
            ),
        ],
    ),
    # ── Skill ──
    (
        SkillOutcome(SkillKind.REFUSED_SYMLINK, repo=REPO, refusal=REPO_REFUSAL),
        [f"  {REPO}: {REPO_REFUSAL.message}"],
    ),
    (
        SkillOutcome(SkillKind.REFUSED_SYMLINK, refusal=REPO_REFUSAL),
        [f"  {REPO_REFUSAL.message}"],
    ),
    (
        SkillOutcome(SkillKind.PRESERVED, base_label="~/.claude/skills"),
        ["  preserved ~/.claude/skills/nauro-* (other nauro projects still registered)"],
    ),
    (SkillOutcome(SkillKind.WROTE, target=TARGET), [f"  wrote {TARGET}"]),
    (SkillOutcome(SkillKind.REMOVED, target=TARGET), [f"  removed {TARGET}"]),
    (SkillOutcome(SkillKind.ABSENT, target=TARGET), [f"  no skill at {TARGET}"]),
    # ── Agent ──
    (
        AgentOutcome(AgentKind.SURFACE_NOT_IMPLEMENTED, surface="cursor"),
        ["  skipped ~/.cursor agents (not yet implemented)"],
    ),
    (
        AgentOutcome(AgentKind.SURFACE_INVALID, surface="x", detail="bad"),
        [f"  skipped agents on surface {'x'!r}: bad"],
    ),
    (
        AgentOutcome(AgentKind.PRESERVED),
        ["  preserved ~/.claude/agents/nauro-* (other nauro projects still registered)"],
    ),
    (
        AgentOutcome(AgentKind.REFUSED_SYMLINK, refusal=USER_REFUSAL),
        [f"  {USER_REFUSAL.message}"],
    ),
    (AgentOutcome(AgentKind.UNCHANGED, target=TARGET), [f"  unchanged {TARGET}"]),
    (AgentOutcome(AgentKind.OVERWROTE, target=TARGET), [f"  overwrote {TARGET}"]),
    (
        AgentOutcome(AgentKind.UPDATED, target=TARGET, backup_name="SKILL.md.bak"),
        [f"  updated {TARGET} (previous saved to SKILL.md.bak)"],
    ),
    (AgentOutcome(AgentKind.INSTALLED, target=TARGET), [f"  installed {TARGET}"]),
    (AgentOutcome(AgentKind.ABSENT, target=TARGET), [f"  no agent at {TARGET}"]),
    (AgentOutcome(AgentKind.REMOVED, target=TARGET), [f"  removed {TARGET}"]),
    (
        AgentOutcome(AgentKind.PRESERVED_MODIFIED, target=TARGET),
        [f"  preserved {TARGET} (locally modified)"],
    ),
]


@pytest.mark.parametrize("outcome, expected", RENDER_CASES)
def test_render_exact_lines(outcome, expected):
    assert render(outcome) == expected


def test_render_covers_every_kind_member():
    """The table must exercise every Kind member so no branch goes unpinned."""
    kind_enums = [
        JsonMcpKind,
        ClaudeHookKind,
        ClaudeUserConfigKind,
        LegacyKind,
        BridgeKind,
        CodexConfigKind,
        CodexHookKind,
        SkillKind,
        AgentKind,
    ]
    covered = {outcome.kind for outcome, _ in RENDER_CASES if hasattr(outcome, "kind")}
    for enum in kind_enums:
        for member in enum:
            assert member in covered, f"{enum.__name__}.{member.name} is not pinned"

    # GitIgnoreResult renders as an attachment on codec outcomes, so its kinds
    # are pinned through the `gitignore=` cases above.
    gitignore_covered = {
        outcome.gitignore.kind
        for outcome, _ in RENDER_CASES
        if getattr(outcome, "gitignore", None) is not None
    }
    for member in GitIgnoreKind:
        assert member in gitignore_covered, f"GitIgnoreKind.{member.name} is not pinned"


def test_render_rejects_unknown_outcome_type():
    with pytest.raises(TypeError):
        render(object())  # type: ignore[arg-type]


# One malformed outcome per codec dispatch: a kind outside its enum must raise
# from the inner match's ``case _`` arm, never fall through to a None echo.
UNRENDERABLE = [
    JsonMcpOutcome(object(), REPO, ".mcp.json"),  # type: ignore[arg-type]
    ClaudeHookOutcome(object(), REPO),  # type: ignore[arg-type]
    ClaudeUserConfigOutcome(object()),  # type: ignore[arg-type]
    LegacyOutcome(object(), REPO),  # type: ignore[arg-type]
    BridgeOutcome(object(), REPO),  # type: ignore[arg-type]
    CodexConfigOutcome(object(), CFG),  # type: ignore[arg-type]
    CodexHookOutcome(object(), REPO),  # type: ignore[arg-type]
    SkillOutcome(object()),  # type: ignore[arg-type]
    AgentOutcome(object()),  # type: ignore[arg-type]
]


@pytest.mark.parametrize("outcome", UNRENDERABLE)
def test_render_unknown_kind_raises(outcome):
    with pytest.raises(TypeError):
        render(outcome)
