# Nauro

Set the direction once. Every agent inherits it.

Your project's direction — goals, decisions, rejected paths — is inherited by every connected agent. When an agent proposes an approach that conflicts with a past decision, Nauro catches the drift before it ships. Works with Claude, Perplexity, ChatGPT, Cursor, and any MCP client.

## Install

```bash
pipx install nauro   # or: pip install nauro
```

Requires Python 3.11+.

## Quickstart

```bash
nauro init --demo
nauro setup claude-code   # writes the MCP entry to ~/.claude/settings.json
```

Open Claude Code and ask:

> "Check if we should add a WebSocket endpoint for live task updates"

The demo creates a sample project with 7 decisions, project state, and open questions. `check_decision` surfaces a conflict: the team already chose SSE over WebSocket because persistent connections weren't released during ECS rolling deploys. No account needed.

For real-project setup, cross-surface access, MCP tool reference, and architecture details, see the [main project README](https://github.com/nauro-ai/nauro#readme).

## Why Nauro?

Memory tools record what agents saw and said. Nauro captures what you decided and rejected, then checks every session against those decisions before they drift.

The `check_decision` → `propose_decision` → `confirm_decision` pipeline catches conflicts before they're written, across any connected surface. Decisions made in Claude Code are validated in Perplexity. No platform vendor owns your context.

## Pricing

Free: unlimited local usage, unlimited projects, 5,000 remote MCP calls/month. See [nauro.ai/pricing](https://nauro.ai/pricing) for hosted tiers.

---

Apache 2.0 license. Part of the [nauro-ai/nauro](https://github.com/nauro-ai/nauro) monorepo.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.
