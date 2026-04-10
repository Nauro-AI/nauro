# Nauro

Persistent project context for AI coding agents.

Nauro maintains versioned project context (decisions, state, open questions) and delivers it to Claude, Perplexity, ChatGPT, Cursor, and any MCP client. Think `git log` for *why* your project is the way it is.

## Install

```bash
pipx install nauro
```

Or with pip:

```bash
pip install nauro
```

Requires Python 3.11+.

## Quickstart

### Try the demo locally (1 minute)

```bash
nauro init --demo
nauro setup claude-code
```

Open Claude Code and ask:

> "What did we decide about the database?"

The demo creates a sample project with three decisions, project state, and open questions. No account needed.

### Try it across surfaces (2 minutes)

To access the same project context from Claude AI, Perplexity, or ChatGPT:

```bash
nauro auth login
nauro sync
```

Then add `mcp.nauro.ai` as a remote MCP connector in your tool's settings. Ask the same question — same answers, different surface.

### Use with your project

```bash
# Register your project
nauro init my-project

# Log a decision directly
nauro note "Chose Postgres over MongoDB for ACID compliance"

# Set your Anthropic API key to auto-extract decisions from commits
nauro config set api_key sk-ant-...
nauro extract
```

## Why Nauro?

| Approach | Cross-tool | Extracted from commits | Versioned | Format |
|---|---|---|---|---|
| **Nauro** | All MCP clients | Yes (via Haiku) | Snapshots + diffs | Portable markdown |
| AGENTS.md (manual) | Tools with repo access | No | Git history only | Markdown |
| Cursor Rules | Cursor only | No | No | Proprietary |
| Claude Memory | Claude only | Partial | No | Proprietary |

`check_decision` catches when a new approach conflicts with a past decision, across any connected surface.

## How it works

A Python CLI extracts decisions from your git history using Haiku, stores them as flat markdown in `~/.nauro/projects/`, and validates new decisions against existing ones (structural screening, embedding similarity, LLM evaluation). An MCP server delivers context to any connected AI tool. Cloud sync keeps everything in sync via S3.

```
~/.nauro/projects/<n>/
  project.md          # goals, constraints
  state.md            # current focus, blockers
  decisions/          # one markdown file per decision
  open-questions.md   # unresolved threads
  snapshots/          # versioned store captures
```

All content is plain markdown. No database, no proprietary format.

## Packages

This repo contains two packages:

| Package | Path | PyPI |
|---|---|---|
| `nauro` | `packages/nauro/` | `pip install nauro` |
| `nauro-core` | `packages/nauro-core/` | `pip install nauro-core` |

Why two packages? `nauro-core` contains the pure-Python parsing, validation, and context assembly logic shared between the CLI and Nauro's hosted remote MCP server. It has zero dependencies and can be used independently by third-party tools that want to read or write the Nauro decision format.

## MCP tools

11 tools (7 read, 4 write) exposed to any connected MCP client:

**Read:**
- `get_context` — project summary at three detail levels (L0/L1/L2)
- `list_decisions` — browse the full decision history
- `get_decision` — full content of a specific decision by number
- `search_decisions` — keyword search across decision titles and rationale
- `get_raw_file` — raw markdown content of any store file
- `diff_since_last_session` — what changed since your last session (or N days ago)
- `check_decision` — check a proposed approach for conflicts without writing

**Write:**
- `propose_decision` / `confirm_decision` — write decisions with conflict validation
- `flag_question` — flag an unresolved question
- `update_state` — report progress

## Your data

**Local extraction (free tier):** Code diffs go directly from your machine to your Anthropic API key. Nauro is never in the data path.

**Cloud sync:** Project context (decisions, state, open questions — not source code) is stored encrypted in AWS S3 (SSE-S3). Each user's data is isolated under a unique prefix.

**Remote MCP:** When connected, your project context is read from S3 and delivered to the AI tool. The AI tool's own data policies apply after delivery.

## Pricing

Free tier: unlimited local usage + 100 remote MCP calls/month. Pro ($9/mo) adds unlimited remote MCP and hosted extraction.

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and [CLAUDE.md](CLAUDE.md) for architecture context.

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q -m "not integration"
```

Apache 2.0 license.
