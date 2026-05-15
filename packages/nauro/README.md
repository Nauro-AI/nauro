# Nauro

Set the doctrine once. Every agent inherits it.

Your project's doctrine — goals, decisions, rejected paths — is inherited by every connected agent. When an agent proposes an approach that conflicts with a past decision, Nauro catches the drift before it ships. Works with Claude, Perplexity, ChatGPT, Cursor, and any MCP client.

## Install

```bash
pipx install nauro   # or: pip install nauro
```

Requires Python 3.10+.

## Quickstart

Watch Nauro catch a conflict in 30 seconds — no account, no MCP wiring, no restart:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check "Add a WebSocket endpoint for live task updates"
```

You'll see:

```
store:    local
project:  demo-project
approach: Add a WebSocket endpoint for live task updates

Related decisions (5):
  004-sse-over-websocket  SSE over WebSocket for live task updates  (score 5.0, status active, decided 2026-03-15)
  002-rest-api-over-graphql  REST API over GraphQL for simplicity  (score 2.4, status active, decided 2026-03-15)
  006-cursor-based-pagination  Cursor-based pagination, not offset  (score 1.3, status active, decided 2026-03-15)
  003-monorepo-with-turborepo  Monorepo with Turborepo over polyrepo  (score 1.0, status active, decided 2026-03-15)
  007-hard-delete-with-audit-log  Hard delete with audit log, no soft deletes  (score 0.5, status active, decided 2026-03-15)

Found 5 related decisions. Top match: D004 "SSE over WebSocket for live task updates" (status active, decided 2026-03-15, BM25 5.0). Call get_decision on each related decision before proposing.

For full rationale, read decision files in ~/.nauro/projects/<project-id>/decisions/, or call the get_decision MCP tool after `nauro setup` + restart.
```

The demo project ruled out WebSocket because persistent connections weren't released during ECS rolling deploys. Without Nauro, any new agent would happily re-propose it.

For real-project setup (`nauro init` / `nauro adopt`), cross-surface access, MCP tool reference, and architecture details, see the [main project README](https://github.com/nauro-ai/nauro#readme). Don't run `nauro setup` from `/tmp/nauro-demo` — that would wire the throwaway demo into your MCP client.

## Why Nauro?

Memory tools record what agents saw and said. Nauro captures what you decided and rejected, then checks every session against those decisions before they drift.

The `check_decision` → `propose_decision` → `confirm_decision` pipeline surfaces conflicts for you to confirm before they're written, across any connected surface. Decisions made in Claude Code surface in Perplexity. No platform vendor owns your context.

## Pricing

Free: unlimited local usage, unlimited projects, 5,000 remote MCP calls/month. See [nauro.ai/pricing](https://nauro.ai/pricing) for hosted tiers.

---

Apache 2.0 license. Part of the [nauro-ai/nauro](https://github.com/nauro-ai/nauro) monorepo.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.
