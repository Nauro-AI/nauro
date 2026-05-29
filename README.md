# Nauro

Nauro gives AI agents persistent project context across Claude, Cursor, Codex, ChatGPT, and any MCP client. It captures decisions and rejected options, then checks new proposals against them so agents stop re-suggesting approaches you already ruled out.

**Status:** Beta. 1.0 will be the first production release.

More at [nauro.ai](https://nauro.ai).

## Install

```bash
pipx install nauro   # or: pip install nauro
```

Requires Python 3.10+.

## Quickstart

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Add a WebSocket endpoint for live task updates"
```

The demo includes a decision that rejected WebSocket for SSE. `nauro check-decision` runs the MCP `check_decision` tool against your local store and surfaces that decision before your agent re-proposes it.

## Adopt Nauro in your project

**New project:**

```bash
nauro init my-project
nauro setup claude-code   # or: nauro setup all
```

`nauro init` writes `.nauro/config.json` into the repo; commit it. For cloud sync from the start, run `nauro auth login` first, then `nauro init --cloud my-project`.

**Existing repo with docs to seed from:**

```bash
nauro adopt
```

`nauro adopt` registers the project, wires MCP across Claude Code, Cursor, and Codex, and installs a `/nauro-adopt` skill. Restart your agent and invoke `/nauro-adopt`.

Add `--with-subagents` on `nauro adopt` or `nauro setup` to install Nauro's bundled Claude Code subagents into `~/.claude/agents/`. The typical workflow:

- `@nauro-planner` before non-trivial work. Drafts a plan and classifies doctrine risk (GREEN/AMBER/RED) against your decision log.
- `@nauro-executor` after a plan is agreed. Implements it, runs tests, opens a PR.
- `@nauro-reviewer` before merging. Audits the diff for real bugs and for missing decision references.
- `@nauro-tech-lead` to set or correct direction. Reads the decision log, audits PRs against doctrine, files decisions when direction is established.

Chat surfaces (Claude.ai, ChatGPT, Perplexity): run `nauro adopt` from a terminal first, then point the chat agent at [`docs/adopt-prompt.md`](docs/adopt-prompt.md).

## Cross-surface sync

```bash
nauro auth login
nauro link --cloud   # one-time: promote the local project to cloud
nauro sync
```

Then add `https://mcp.nauro.ai/mcp` as an MCP connector in your tool's settings.

Codex users: add `mcp_oauth_callback_port = 8765` to the top of `~/.codex/config.toml` so the OAuth callback uses a fixed port.

## MCP tools

11 tools total (8 read, 3 write). The local stdio server registers 10 (7 read, 3 write); `list_projects` is remote-only.

**Read:** `check_decision`, `get_context`, `list_decisions`, `get_decision`, `search_decisions`, `get_raw_file`, `diff_since_last_session`, `list_projects` *(remote-only)*.

**Write:** `propose_decision`, `flag_question`, `update_state`.

`nauro check-decision "<approach>"` runs `check_decision` from the shell. The write tools surface the same way:

```bash
nauro propose-decision "Adopt Redis" "In-memory cache for hot read paths" \
    --files-affected src/cache.py --files-affected src/api.py \
    --rejected '[{"alternative": "Memcached", "reason": "Less feature-rich"}]'
```

Repeat `--files-affected` for each entry. `--rejected` accepts inline JSON, `@file.json`, or `-` to read from stdin.

## Packages

| Package | Path | PyPI |
|---|---|---|
| `nauro` | `packages/nauro/` | `pip install nauro` |
| `nauro-core` | `packages/nauro-core/` | `pip install nauro-core` |

`nauro-core` is the parsing, validation, and context assembly shared between the CLI and the hosted MCP server. Minimal dependencies; usable by third-party tools that read or write the Nauro decision format.

The hosted MCP server (`mcp.nauro.ai`) lives in a private repository.

## Development

See [CLAUDE.md](CLAUDE.md) for architecture. Bugs and feature requests: [GitHub Issues](https://github.com/Nauro-AI/nauro/issues).

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q
```

---

Apache 2.0.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.
