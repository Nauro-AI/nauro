# Nauro

A decision system for agentic engineering.

Catch the moment an agent re-proposes something you already ruled out. Your project's decisions, including what you chose and what you ruled out, travel with every connected agent. When an agent proposes an approach, Nauro surfaces the past decisions related to it, so the agent sees the prior reasoning before it writes code. The check is advisory: it never blocks, and you approve anything that gets recorded. Works with Claude, Perplexity, Cursor, and any MCP client.

## Install

```bash
uv tool install nauro     # uv fetches its own Python — nothing else needed
```

No `uv`? Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or the [PowerShell line](https://docs.astral.sh/uv/getting-started/installation/) on Windows. Already on Python 3.10+? `pipx install nauro` (or `pip install nauro`) works too.

## Quickstart

Catch a conflict in about 30 seconds. No account, MCP wiring, or restart required:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Add a WebSocket endpoint for live task updates"
```

You'll see a JSON envelope with the related decisions and a deterministic assessment, e.g.:

```json
{
  "store": "local",
  "related_decisions": [
    {
      "id": "decision-004",
      "title": "SSE over WebSocket for live updates",
      "score": 6.117,
      "status": "active",
      "date": "2026-03-15",
      "rationale_preview": "Server-Sent Events (SSE) for pushing live task updates..."
    }
  ],
  "assessment": "Found 5 related decisions. Top match: D004 \"SSE over WebSocket for live updates\"..."
}
```

The demo project ruled out WebSocket because persistent connections weren't released during ECS rolling deploys. Without Nauro, a fresh agent has no record of that and would re-propose WebSocket.

For real-project setup (`nauro init` / `nauro adopt`), cross-surface access, MCP tool reference, and architecture details, see the [main project README](https://github.com/nauro-ai/nauro#readme). Don't run `nauro setup` from `/tmp/nauro-demo`; that would wire the throwaway demo into your MCP client.

`nauro adopt --with-subagents` additionally installs Nauro's bundled Claude Code workflow subagents (`@nauro-planner`, `@nauro-executor`, `@nauro-reviewer`, `@nauro-tech-lead`) into `~/.claude/agents/`. Off by default to avoid overwriting locally-customized files; pass `--force-overwrite` to replace customized files.

## Why Nauro?

Nauro is decisional, not observational. It captures what you decided and what you ruled out, with the reasoning. When an agent proposes a change, a keyword search over those decisions surfaces the relevant ones, so the prior reasoning is in front of the agent at proposal time.

No model judges your decisions. The check uses deterministic keyword retrieval (BM25), is advisory, and never blocks a change. You approve every decision before it is recorded.

`check_decision` returns the related prior decisions (the `related_decisions` list shown above) so the agent can weigh them before proposing; Nauro ranks by keyword relevance and does not judge whether they conflict. When you record a choice with `propose_decision`, near-matches surface as advisory `similar_decisions` on the same call, and a clean proposal commits in one call. What you decide in one tool, every connected agent inherits; for example, a decision recorded in Claude Code is available later in Perplexity. The store is plain markdown in a folder you own. Run it fully locally with no account; cloud sync is opt-in.

## Pricing

Free: unlimited local usage, unlimited projects, 5,000 remote MCP calls/month. See [nauro.ai/pricing](https://nauro.ai/pricing) for hosted tiers.

---

Apache 2.0 license. Part of the [nauro-ai/nauro](https://github.com/nauro-ai/nauro) monorepo.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.
