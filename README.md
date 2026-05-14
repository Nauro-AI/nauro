# Nauro

Set the doctrine once. Every agent inherits it.

Nauro is an open-source system that gives AI agents persistent project context across tools and sessions.

It captures a project's decisions, rejected options, rationale, constraints, current state, and open questions, then makes that context available across Claude, Perplexity, Cursor, Codex, and any MCP client.

Nauro is designed for individuals and teams using multiple agents across coding, research, planning, documentation, and product work.

When an agent proposes work that conflicts with a recorded decision, Nauro surfaces the conflict in the session, before it ships.

## Why Nauro

Projects accumulate context. Agents start from scratch.

Your project has goals, constraints, decisions, rejected options, open questions, and rationale. But every new agent session sees only a slice of that history, so you keep re-explaining the same background and correcting ideas you already ruled out.

As work moves across Claude, Perplexity, Cursor, Codex, ChatGPT, and other MCP clients, the problem gets worse. A decision made in one surface may be invisible in another. An approach that failed last month can look reasonable in isolation today.

The context resets. The drift does not.

Nauro gives agents a shared project record, then checks new proposals against the decisions already on file.

## Install

```bash
pipx install nauro   # or: pip install nauro
```

Requires Python 3.10+.

## Quickstart

Try the demo locally in 2 minutes — no account needed.

1. `nauro init --demo` — scaffold a sample project with 7 decisions, project state, and open questions.
2. `nauro setup claude-code` — write the MCP entry to `~/.claude/settings.json`.
3. Open Claude Code and ask: *"Check if we should add a WebSocket endpoint for live task updates"*

`check_decision` surfaces a conflict: the team already chose SSE over WebSocket because persistent connections weren't released during ECS rolling deploys.

## Use with your project

```bash
nauro init my-project
nauro note "All processing stays in the request path, no background workers. Async queue added 3 failure modes we couldn't monitor in v1"
nauro setup claude-code
```

`nauro init` writes a small `.nauro/config.json` into your repo (commit it — it links the repo to the project so any `nauro` command you run from inside this repo knows which project to use). To start with cloud sync from day one, use `nauro init --cloud my-project` instead.

Agents propose decisions directly through MCP during sessions. When a proposal overlaps with existing decisions, you confirm before it's written.

## Adopt an existing repo

For a repo that already has docs — README, ADRs, Memory-Bank files, manifests:

```bash
nauro adopt
```

Run from the repo root. `nauro adopt` creates the project, wires MCP across your installed agents (Claude Code, Cursor, Codex), and drops a portable skill into your agent. Invoke `/nauro-adopt` in any connected agent and it reads your existing docs, surfaces decision candidates for you to keep or skip, then seeds the store one decision at a time. The agent does the reading — no API key required server-side.

## Use across surfaces

Cross-surface access requires authenticating to the hosted MCP — your decisions live in your own S3 prefix so any tool can reach them.

```bash
nauro auth login
nauro link --cloud   # one-time: promote local project to cloud
nauro sync
```

Then add `https://mcp.nauro.ai/mcp` as a remote MCP connector in your tool's settings. Ask the same question, same answers, different surface.

Codex users: also add `mcp_oauth_callback_port = 8765` to the top of `~/.codex/config.toml` so the OAuth callback uses a fixed port.

## How it works

Agents propose decisions through MCP during sessions. Proposals are drafted in collaboration with your agent; when one overlaps with an existing decision, you confirm before it's written, and updates or replacements of existing decisions always require explicit confirmation. The gate is the boundary between "we discussed it" and "your project's doctrine has changed." You can also log decisions from the terminal with `nauro note`. Open questions are tracked too, so agents surface unresolved tensions before they become assumptions.

One project spans many repos — the store lives in `~/.nauro/`, not inside any repo, so context follows the project across the whole codebase.

Everything is stored as flat markdown in `~/.nauro/projects/` and matched against existing decisions (structural screening and BM25 retrieval); you confirm whether to write. Cloud sync replicates the local store to S3 for cross-device and remote MCP access.

```
~/.nauro/projects/<name>/
  project.md          # goals, constraints
  stack.md            # languages, frameworks, infrastructure
  state.md            # current focus, blockers
  decisions/          # one markdown file per decision
  open-questions.md   # unresolved threads
  snapshots/          # versioned store captures
```

All content is plain markdown. No database, no proprietary format.

## How it compares

Nauro is decisional, not observational. Memory tools preserve conversation history; Nauro records the decisions and rejected options that conversation produced, and `check_decision` matches every new proposal against that record before it lands.

| Approach | What it captures | Cross-tool reach | Validates against past decisions |
|---|---|---|---|
| **Nauro** | Decisions with rationale | Any MCP client | Yes (`check_decision`) |
| AGENTS.md / ADRs (manual) | Decisions, manually maintained | Tools with repo access | No |
| Cursor Rules | Coding preferences | Cursor only | No |
| Memory tools (mem0, Letta, Zep) | Conversation history | Per integration | No |
| Platform memory (Copilot, Windsurf, Claude) | Usage patterns | Single vendor | No |

The `check_decision` → `propose_decision` → `confirm_decision` pipeline surfaces conflicts for you to confirm before they're written, across any connected surface. A decision recorded from one connected tool surfaces in every other one. Your decisions stay yours, not your platform's.

## Your data

**Local usage (free tier):** Everything runs on your machine. The store lives under `~/.nauro/` and never leaves the device unless you turn on cloud sync.

**Cloud sync:** Project context (decisions, state, open questions — not source code) is stored encrypted in AWS S3 (SSE-S3). Each user's data is isolated under a unique prefix.

**Remote MCP:** When connected, your project context is read from S3 and delivered to the AI tool. The AI tool's own data policies apply after delivery.

## Pricing

Free: unlimited local usage, unlimited projects, 5,000 remote MCP calls/month. A typical agent session uses 10–30 calls, so the free tier covers roughly 200+ sessions a month. See [nauro.ai/pricing](https://nauro.ai/pricing) for hosted tiers.

## MCP tools

12 tools (8 read, 4 write) exposed to any connected MCP client:

**Read:**
- `check_decision` — check a proposed approach against existing decisions without writing (the centerpiece; agents are instructed to call this before any architectural change)
- `get_context` — project summary at three detail levels (L0/L1/L2)
- `list_decisions` — browse the full decision history
- `get_decision` — full content of a specific decision by number
- `search_decisions` — keyword search across decision titles and rationale (BM25)
- `get_raw_file` — raw markdown content of any store file
- `diff_since_last_session` — what changed since your last session (or N days ago)
- `list_projects` — list projects you have access to (only needed when you have multiple — single-project users auto-resolve)

**Write:**
- `propose_decision` / `confirm_decision` — write decisions with conflict validation
- `flag_question` — flag an unresolved question
- `update_state` — report progress

## Contributing

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and [CLAUDE.md](CLAUDE.md) for architecture context.

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q -m "not integration"
```

## Packages

This repo contains two packages:

| Package | Path | PyPI |
|---|---|---|
| `nauro` | `packages/nauro/` | `pip install nauro` |
| `nauro-core` | `packages/nauro-core/` | `pip install nauro-core` |

`nauro-core` contains the pure-Python parsing, validation, and context assembly logic shared between the CLI and the hosted remote MCP server. Minimal dependencies (BM25 search only) so it can be used independently by third-party tools that want to read or write the Nauro decision format.

---

Apache 2.0 license.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.
