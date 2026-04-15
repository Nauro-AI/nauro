# Nauro

Give every AI agent your project's theory: the decisions, rationale, and rejected paths.

Nauro maintains versioned project context and delivers it to Claude Code, Claude AI, Perplexity, ChatGPT, Cursor, and any MCP client. When an agent proposes an approach that conflicts with a past decision, Nauro catches it before the drift happens.

Named for Peter Naur, whose 1985 paper argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.

## Install

```bash
pipx install nauro   # or: pip install nauro
```

Requires Python 3.11+.

## Quickstart

### Try the demo locally (2 minutes)

```bash
nauro init --demo
nauro setup claude-code
```

Open Claude Code and ask:

> "Check if we should add a WebSocket endpoint for live task updates"

The demo creates a sample project with 7 decisions, project state, and open questions. `check_decision` surfaces a conflict: the team already chose SSE over WebSocket because persistent connections weren't released during ECS rolling deploys. No account needed.

### Try it across surfaces

To access the same project context from Claude AI, Perplexity, or ChatGPT:

```bash
nauro auth login
nauro sync
```

Then add `mcp.nauro.ai` as a remote MCP connector in your tool's settings. Ask the same question, same answers, different surface.

### Use with your project

```bash
nauro init my-project
nauro note "All processing stays in the request path, no background workers. Async queue added 3 failure modes we couldn't monitor in v1"
nauro setup claude-code
```

Agents can also propose decisions directly through MCP during sessions. To bootstrap from existing git history, set an API key and run `nauro extract`.

## Why Nauro?

Memory tools record what agents saw and said. Nauro captures what you decided and rejected, then checks every session against those decisions before they drift.

| Approach | Cross-tool | Decisions validated | Versioned | Format |
|---|---|---|---|---|
| **Nauro** | All MCP clients | Yes (`check_decision`) | Snapshots + diffs | Portable markdown |
| AGENTS.md (manual) | Tools with repo access | No | Git history only | Markdown |
| Cursor Rules | Cursor only | No | No | Proprietary |
| Claude Memory | Claude only | No | No | Proprietary |

The `propose_decision` → `confirm_decision` → `check_decision` pipeline catches conflicts across any connected surface. Decisions made in Claude Code are validated in Perplexity. No platform vendor owns your context.

## How it works

Agents propose decisions through MCP during sessions. You can also log decisions from the terminal with `nauro note` or bootstrap from git history with `nauro extract`. Everything is stored as flat markdown in `~/.nauro/projects/` and validated against existing decisions (structural screening, BM25 retrieval, LLM evaluation). Cloud sync replicates the local store to S3 for cross-device and remote MCP access.

```
~/.nauro/projects/<n>/
  project.md          # goals, constraints
  stack.md            # languages, frameworks, infrastructure
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

`nauro-core` contains the pure-Python parsing, validation, and context assembly logic shared between the CLI and the hosted remote MCP server. Minimal dependencies (BM25 search only) so it can be used independently by third-party tools that want to read or write the Nauro decision format.

## MCP tools

11 tools (7 read, 4 write) exposed to any connected MCP client:

**Read:**
- `get_context` — project summary at three detail levels (L0/L1/L2)
- `list_decisions` — browse the full decision history
- `get_decision` — full content of a specific decision by number
- `search_decisions` — keyword search across decision titles and rationale (BM25)
- `get_raw_file` — raw markdown content of any store file
- `diff_since_last_session` — what changed since your last session (or N days ago)
- `check_decision` — check a proposed approach for conflicts without writing

**Write:**
- `propose_decision` / `confirm_decision` — write decisions with conflict validation
- `flag_question` — flag an unresolved question
- `update_state` — report progress

## Your data

**Local usage (free tier):** Everything runs on your machine. If you use `nauro extract`, code diffs go directly to your Anthropic API key. Nauro is never in the data path.

**Cloud sync:** Project context (decisions, state, open questions — not source code) is stored encrypted in AWS S3 (SSE-S3). Each user's data is isolated under a unique prefix.

**Remote MCP:** When connected, your project context is read from S3 and delivered to the AI tool. The AI tool's own data policies apply after delivery.

## Pricing

Free tier: unlimited local usage, unlimited projects, 5,000 remote MCP calls/month. Pro tier coming soon with unlimited remote MCP and hosted extraction.

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and [CLAUDE.md](CLAUDE.md) for architecture context.

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q -m "not integration"
```

Apache 2.0 license.
