"""Drift tests for the canonical bundled subagent bodies.

The canonical bodies live at
``packages/nauro/src/nauro/agents/<name>.md``. ``load_agent_body(name)``
returns each body via importlib.resources. ``render_agent(surface, name)``
is the single source of truth for what the materializer writes into the
user's surface directory.

Subagent markdown ships with full Claude Code frontmatter inline, so
``render_agent("claude_code", name)`` returns the body unchanged; there
is no per-surface wrapping. Codex renders the canonical instructions into
custom-agent TOML. Cursor raises ``NotImplementedError`` until a target shape
is defined.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nauro.agents import AGENT_NAMES, emit_plugin_agents, load_agent_body, render_agent

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "nauro" / "agents"
REPO_ROOT = Path(__file__).resolve().parents[3]


AGENT_ANCHORS: dict[str, tuple[str, ...]] = {
    "nauro-planner": (
        "Doctrine triage",
        "GREEN, AMBER, or RED",
        "REFUSE TO DRAFT",
    ),
    "nauro-executor": (
        "Stay in scope",
        "Run `ruff format` and `ruff check`",
        "Local completion — do not push",
    ),
    "nauro-reviewer": (
        "VERDICT: APPROVE | BLOCK | APPROVE WITH NITS",
        "Hard rules (BLOCK if any fail)",
        "Code review — find real bugs",
    ),
    "nauro-tech-lead": (
        "How to run — three modes",
        "Never file without user approval.",
        "Mode A: Direction-setting",
    ),
}


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_load_agent_body_returns_canonical_bytes(name: str) -> None:
    body = load_agent_body(name)
    assert body.endswith("\n")
    assert 1000 < len(body) < 30000
    for anchor in AGENT_ANCHORS[name]:
        assert anchor in body, f"missing anchor {anchor!r} in {name}.md"


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_render_agent_claude_code_returns_body(name: str) -> None:
    rendered = render_agent("claude_code", name)
    assert rendered == load_agent_body(name)
    assert rendered.startswith(f"---\nname: {name}\n")


# Retired tool / mechanism names that must never reappear in a rendered agent
# body. ``confirm_decision`` was removed when propose_decision collapsed to a
# single-call commit; a stale reference would tell the subagent to call a tool
# that no longer exists.
RETIRED_AGENT_PHRASES: tuple[tuple[str, str], ...] = (
    ("confirm_decision", "confirm_decision was removed; propose_decision is a single-call commit"),
)


@pytest.mark.parametrize("name", list(AGENT_NAMES))
@pytest.mark.parametrize("phrase,reason", RETIRED_AGENT_PHRASES)
def test_agent_body_has_no_retired_phrases(name: str, phrase: str, reason: str) -> None:
    assert phrase not in render_agent("claude_code", name), (
        f"retired phrase {phrase!r} found in {name}.md: {reason}"
    )


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_render_agent_cursor_raises_not_implemented(name: str) -> None:
    with pytest.raises(NotImplementedError):
        render_agent("cursor", name)


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_render_agent_codex_preserves_canonical_instructions(name: str) -> None:
    rendered = render_agent("codex", name)
    data = tomllib.loads(rendered)
    canonical = load_agent_body(name)
    end = canonical.find("\n---\n", 4)
    frontmatter = canonical[4:end]
    description = next(
        line.split(":", 1)[1].strip()
        for line in frontmatter.splitlines()
        if line.startswith("description:")
    )

    assert data["name"] == name
    assert data["description"] == description
    assert data["developer_instructions"] == canonical[end + len("\n---\n") :]
    if name == "nauro-executor":
        assert "sandbox_mode" not in data
    else:
        assert data["sandbox_mode"] == "read-only"


def test_render_agent_unknown_surface_raises_value_error() -> None:
    with pytest.raises(ValueError):
        render_agent("emacs", "nauro-planner")


def test_render_agent_unknown_agent_raises_value_error() -> None:
    with pytest.raises(ValueError):
        render_agent("claude_code", "nauro-architect")


def test_load_agent_body_unknown_agent_raises_value_error() -> None:
    with pytest.raises(ValueError):
        load_agent_body("nauro-architect")


def test_agent_names_matches_files_on_disk() -> None:
    """Every shipped ``.md`` is registered, and every registered name has a file.

    Drift guard: catches both "file added on disk but missing from the
    ``AGENT_NAMES`` tuple" and the reverse.
    """
    on_disk = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))
    registered = sorted(AGENT_NAMES)
    assert on_disk == registered


def test_public_surface_pointer_rule_stays_in_agent_and_pr_guidance() -> None:
    phrase = "Public-facing PR bodies, commits, docs, code comments, schema text, and branch names"
    for name in ("nauro-executor", "nauro-reviewer"):
        assert phrase in load_agent_body(name)

    reviewer = load_agent_body("nauro-reviewer")
    reviewer_instruction = (
        "4. **Hard rule check** against the diff and the drafted PR body. Reject raw decision "
        "or question ids on public surfaces, then call `get_decision` for each remaining "
        "internal decision reference and confirm it resolves."
    )
    stale_instruction = (
        "4. **Hard rule check** against the diff and the drafted PR body. For every decision "
        "reference, call `get_decision` and confirm it resolves."
    )
    assert reviewer.count(reviewer_instruction) == 1
    assert stale_instruction not in reviewer

    template = (REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    assert phrase in template
    assert "Reference Nauro decisions by number" not in template


@pytest.mark.parametrize("name", list(AGENT_NAMES))
def test_all_agent_files_have_required_frontmatter(name: str) -> None:
    """Every shipped agent file parses with the keys Claude Code expects.

    ``name`` must equal the filename minus ``.md``. ``description`` and
    ``model`` are required on every file. ``tools`` is required on agents
    that restrict tool access; the non-filing agents (``nauro-executor``
    and ``nauro-reviewer``) carry a ``tools`` allowlist that excludes the
    store-write tools.
    """
    body = load_agent_body(name)
    assert body.startswith("---\n"), f"{name}.md is missing opening frontmatter fence"
    end = body.find("\n---\n", 4)
    assert end > 0, f"{name}.md frontmatter is not terminated"
    frontmatter = body[4:end]

    keys = {
        line.split(":", 1)[0].strip()
        for line in frontmatter.splitlines()
        if line and not line.startswith(" ")
    }
    assert "name" in keys
    assert "description" in keys
    assert "model" in keys

    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            assert line.split(":", 1)[1].strip() == name, (
                f"frontmatter name does not match filename for {name}.md"
            )
            break


# Store-write tools that must never appear in the tools allowlist of an agent
# that is not authorized to file doctrine.
DOCTRINE_WRITE_TOOLS: tuple[str, ...] = (
    "propose_decision",
    "flag_question",
    "update_state",
)

# The same local stdio server is reachable under two MCP namespaces depending
# on how it was wired: `mcp__nauro__*` when `nauro setup` writes .mcp.json,
# and `mcp__plugin_nauro_nauro__*` when the Claude Code plugin declares it.
# Allowlist mismatches drop tools silently, so every agent must grant both
# namespaces as exact mirrors or plugin-only installs get toolless agents.
LOCAL_NAMESPACE = "mcp__nauro__"
PLUGIN_NAMESPACE = "mcp__plugin_nauro_nauro__"


def _tools_allowlist(name: str) -> list[str]:
    body = load_agent_body(name)
    end = body.find("\n---\n", 4)
    assert end > 0, f"{name}.md frontmatter is not terminated"
    frontmatter = body[4:end]
    tools_line = next(
        (line for line in frontmatter.splitlines() if line.startswith("tools:")),
        None,
    )
    assert tools_line is not None, f"{name}.md frontmatter is missing a tools allowlist"
    return [tool.strip() for tool in tools_line.split(":", 1)[1].split(",")]


@pytest.mark.parametrize("name", AGENT_NAMES)
def test_plugin_namespace_mirrors_local_namespace(name: str) -> None:
    """Each agent grants the plugin MCP namespace as an exact mirror of the local one."""
    tools = _tools_allowlist(name)
    local = {tool[len(LOCAL_NAMESPACE) :] for tool in tools if tool.startswith(LOCAL_NAMESPACE)}
    plugin = {tool[len(PLUGIN_NAMESPACE) :] for tool in tools if tool.startswith(PLUGIN_NAMESPACE)}
    assert local, f"{name}.md grants no {LOCAL_NAMESPACE}* tools"
    assert plugin == local, (
        f"{name}.md plugin-namespace tools do not mirror the local namespace: "
        f"missing {sorted(local - plugin)}, extra {sorted(plugin - local)}"
    )


@pytest.mark.parametrize("name", ["nauro-executor", "nauro-reviewer"])
def test_non_filing_agents_cannot_write_doctrine(name: str) -> None:
    """Executor and reviewer carry a ``tools`` allowlist with no store-write tools.

    The allowlist is the structural guarantee that these agents cannot file
    doctrine; the prose in their bodies is defense-in-depth. Planner and
    tech-lead legitimately carry the write tools, so they are out of scope.
    """
    body = load_agent_body(name)
    end = body.find("\n---\n", 4)
    assert end > 0, f"{name}.md frontmatter is not terminated"
    frontmatter = body[4:end]

    tools_line = next(
        (line for line in frontmatter.splitlines() if line.startswith("tools:")),
        None,
    )
    assert tools_line is not None, f"{name}.md frontmatter is missing a tools allowlist"

    for tool in DOCTRINE_WRITE_TOOLS:
        assert tool not in tools_line, f"{name}.md tools allowlist grants store-write tool {tool!r}"


def test_planner_gates_every_decision_operation_on_explicit_user_approval() -> None:
    body = load_agent_body("nauro-planner")

    assert "all three operations: `add`, `update`, and `supersede`" in body
    assert "A planner subagent without a user channel never files directly." in body
    assert "re-invokes the planner with the user's explicit approval" in body
    assert "On a standalone invocation, show the complete draft and return without filing" in body
    assert "related decisions and assessment from `check_decision`" in body


def test_tech_lead_gates_every_decision_operation_on_explicit_user_approval() -> None:
    body = load_agent_body("nauro-tech-lead")

    assert "every `add`, `update`, and `supersede`" in body
    assert "Standalone" in body
    assert "AskUserQuestion" in body
    assert "Inside the `nauro-ship-task` chain" in body
    assert "return the complete draft to the parent and do not file in-run" in body
    assert "Mode B never files an `add` directly from the transcript." in body
    assert "related decisions and assessment from `check_decision`" in body
    assert "For each real architectural decision identified in step 3" in body
    assert "If the transcript has no `check_decision` precedent" in body
    assert "For an existing or retroactive check, call `get_decision`" in body


def test_tech_lead_mode_c_preserves_surface_first_merge_posture() -> None:
    body = load_agent_body("nauro-tech-lead")

    assert "SURFACE the drift first" in body
    assert "Hold the merge for a landed supersede only when" in body
    assert "frozen public surface" in body
    assert "or write it into the project store" in body
    assert "otherwise the human may merge" in body


# --- plugin emitter + render-plugin command -------------------------------
#
# A separate plugin repo commits byte-identical copies of the subagents and
# verifies them against the live render in its CI (the cross-repo
# byte-identity gate). ``emit_plugin_agents`` is the single canonical source
# it renders from; ``nauro render-plugin --check`` is the verification entry
# point. ``AGENT_NAMES`` / ``render_agent`` are load-bearing public API for
# that gate and must stay in ``__all__``.

runner = CliRunner()


def test_emit_plugin_agents_writes_canonical_bodies(tmp_path: Path) -> None:
    written = emit_plugin_agents(tmp_path)
    assert {p.name for p in written} == {f"{name}.md" for name in AGENT_NAMES}
    for name in AGENT_NAMES:
        target = tmp_path / "agents" / f"{name}.md"
        assert target.read_text(encoding="utf-8") == render_agent("claude_code", name)


def test_emit_plugin_agents_writes_only_agents_subtree(tmp_path: Path) -> None:
    emit_plugin_agents(tmp_path)
    # Only the agents/ subtree is created under dest, nothing else.
    assert {p.name for p in tmp_path.iterdir()} == {"agents"}
    agents_dir = tmp_path / "agents"
    assert {p.stem for p in agents_dir.glob("*.md")} == set(AGENT_NAMES)
    assert {p.name for p in agents_dir.iterdir()} == {f"{name}.md" for name in AGENT_NAMES}


def test_public_api_import_contract() -> None:
    """``render_agent`` and ``AGENT_NAMES`` are the cross-repo gate's contract."""
    import nauro.agents as agents_module

    assert "AGENT_NAMES" in agents_module.__all__
    assert "render_agent" in agents_module.__all__
    assert agents_module.AGENT_NAMES == AGENT_NAMES
    assert agents_module.render_agent is render_agent


def test_render_plugin_check_passes_on_freshly_emitted_tree(tmp_path: Path) -> None:
    from nauro.cli.main import app

    emit_plugin_agents(tmp_path)
    result = runner.invoke(app, ["render-plugin", str(tmp_path), "--check"])
    assert result.exit_code == 0


def test_render_plugin_check_fails_on_drift_and_writes_nothing(tmp_path: Path) -> None:
    from nauro.cli.main import app

    emit_plugin_agents(tmp_path)
    mutated = tmp_path / "agents" / f"{AGENT_NAMES[0]}.md"
    original = mutated.read_text(encoding="utf-8")
    mutated.write_text(original + "drift\n", encoding="utf-8")

    others = {
        name: (tmp_path / "agents" / f"{name}.md").read_text(encoding="utf-8")
        for name in AGENT_NAMES[1:]
    }

    result = runner.invoke(app, ["render-plugin", str(tmp_path), "--check"])
    assert result.exit_code == 1
    # --check writes nothing: the mutated file is left as-is, untouched files unchanged.
    assert mutated.read_text(encoding="utf-8") == original + "drift\n"
    for name, content in others.items():
        assert (tmp_path / "agents" / f"{name}.md").read_text(encoding="utf-8") == content
