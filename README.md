# Nauro

Nauro gives AI agents persistent project context across Claude, Cursor, Codex, ChatGPT, and any MCP client. It captures decisions and rejected options, then checks new proposals against them so agents stop re-suggesting approaches you already ruled out.

[![PyPI](https://img.shields.io/pypi/v/nauro.svg)](https://pypi.org/project/nauro/) [![Python](https://img.shields.io/pypi/pyversions/nauro.svg)](https://pypi.org/project/nauro/) [![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

https://github.com/user-attachments/assets/9e6c475b-c584-470b-84c2-12f01b3a425a

*A coding agent checks the team's prior decisions before it answers, then records the new one you approved and makes the change. Captured in Codex.*

**Status:** Beta (pre-1.0). The local CLI and stdio MCP server are stable; cloud sync is stabilizing.

More at [nauro.ai](https://nauro.ai).

## How it works

Nauro stores your project's decisions as plain markdown files, each with the alternatives you ruled out and the reasoning behind them. When an agent proposes an approach, `check_decision` runs deterministic keyword retrieval (BM25) over those files and surfaces the related ones to the agent before it writes code.

No model judges your decisions. The check is advisory and never blocks a change. You approve every decision before it is recorded. The store is a folder you own; remove Nauro and the markdown stays.

## Install

```bash
pipx install nauro   # or: pip install nauro
```

Requires Python 3.10+.

## Quickstart

No account, no MCP wiring, no restart:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Add a WebSocket endpoint for live task updates"
```

The demo store holds seven real decisions. One of them ruled out WebSocket in favor of SSE, and `check-decision` surfaces it as the top match before your agent can re-propose it:

```json
{
  "store": "local",
  "related_decisions": [
    {
      "id": "decision-004",
      "title": "SSE over WebSocket for live task updates",
      "score": 5.015,
      "status": "active",
      "date": "2026-03-15",
      "rationale_preview": "Server-Sent Events (SSE) for pushing live task updates to the frontend. SSE uses standard HTTP, reconnects automatically on disconnect, and works through every proxy and load balancer..."
    }
  ],
  "assessment": "Found 5 related decisions. Top match: D004 \"SSE over WebSocket for live task updates\" (status active, decided 2026-03-15, BM25 5.0). Call get_decision on each related decision before proposing.",
  "project": { "id": "01K...", "name": "demo-project" }
}
```

Output abbreviated to the top match; the live call returns all five related decisions, ranked by score. The same result reaches your agent through the MCP `check_decision` tool, so it sees the prior decision in the flow rather than after the fact.

## Why not ADRs, grep, or CLAUDE.md?

A decision log in your repo is a good record. The gap is on the read side: a file is read when a person opens it, and a fresh agent session starts with no knowledge that it exists. Nauro closes that gap. The relevant decision reaches your agent automatically, through MCP, at the moment it proposes a change. The store lives outside any single repo, so one record is shared across every repo and tool instead of being trapped in one project's history.

## When Nauro helps, and when it doesn't

Nauro pays off when decisions recur across sessions, when more than one agent or developer touches the same project, and when re-litigating a settled choice is costly.

The limits are worth knowing. It surfaces only what has been recorded as a decision. It adds an MCP round-trip to the agent's flow. Retrieval is keyword-based, which is fast, offline, and auditable, and can miss a decision phrased in different words than the proposal; an optional embeddings index is available for closer synonym matching.

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

## Cross-surface sync (optional)

The steps above work fully on your machine with no account. To sync a project to the cloud and reach it from surfaces without a local copy (claude.ai web) or from another machine:

```bash
nauro auth login
nauro link --cloud   # one-time: promote the local project to cloud
nauro sync
```

Then add `https://mcp.nauro.ai/mcp` as an MCP connector in your tool's settings.

### Codex (remote connector)

Add it under a name distinct from the local `nauro` stdio server:

```bash
codex mcp add nauro-cloud --url https://mcp.nauro.ai/mcp
```

Then pin the OAuth callback port at the top of `~/.codex/config.toml`. Codex's callback uses a fixed, pre-registered port; without it Codex picks a random port and login fails:

```toml
mcp_oauth_callback_port = 8765
```

Requires Codex 0.131.0 or newer. Enter the URL exactly as shown, with no trailing slash. If login reports a callback-port error, free port 8765.

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
