# Nauro

Give your agents the context code leaves out.

Nauro keeps current state, open questions, and human-approved project judgment in one record, ready for every agent you connect.

Nauro keeps a living project record. It combines project scope, current state, and open questions with human-approved project judgment: intent, goals, decisions, rationale, tradeoffs, and rejected paths. Project judgment is the human-ratified part of the record; context is the relevant slice of the record an agent receives for the work in front of it. Works with Claude, Perplexity, Cursor, Codex, and any MCP client.

## How the loop works

1. Nauro orients the agent with project scope, current state, open questions, and relevant prior judgment.
2. You and the agent clarify missing intent, constraints, or tradeoffs.
3. If the work needs new or revised judgment, the agent drafts it and waits for your explicit approval.
4. The agent plans, recommends, or implements with that context in view.
5. The agent explains how the context shaped the result, and you accept, correct, except, reopen, or supersede it in conversation.
6. The agent reports meaningful completed progress as current state, so later connected agents inherit the updated state and approved judgment.

The markdown store, context summaries, BM25 retrieval, advisory checks, and optional sync support this loop. They do not replace your judgment or silently change project truth.

## Install

```bash
uv tool install nauro     # uv fetches its own Python — nothing else needed
```

No `uv`? Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or the [PowerShell line](https://docs.astral.sh/uv/getting-started/installation/) on Windows. Already on Python 3.10+? `pipx install nauro` (or `pip install nauro`) works too.

## Quickstart

See a prior decision in about 30 seconds. No account, MCP wiring, or restart required:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Store dollar amounts as decimal numbers"
```

`nauro init --demo` also generates `AGENTS.md` in the current directory so a coding agent can load the Nauro preflight and demo context before its first task.

You'll see a JSON envelope with the related decisions and a deterministic assessment, e.g.:

```json
{
  "store": "local",
  "related_decisions": [
    {
      "id": "decision-001",
      "title": "Amounts stored in integer cents, never floating point",
      "score": 8.462,
      "status": "active",
      "date": "2026-03-15",
      "rationale_preview": "Every monetary amount (transactions, budgets, balances) is stored as an integer number of cents and formatted to dollars only for display..."
    }
  ],
  "assessment": "Found 5 related decisions. Top match: D001 \"Amounts stored in integer cents, never floating point\"..."
}
```

The demo project ruled out storing money as floating-point dollars because binary floating point cannot represent a value like 0.10 exactly, so totals accumulate rounding error and a balance that should read 0.00 shows -0.01. This protective example isolates Nauro's retrieval mechanism: it brings a recorded constraint into the proposal flow before an agent can re-propose the rejected field.

If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, Nauro may be more than you need. Nauro is designed for context that must persist across longer histories, sessions, tools, repos, machines, or repeated handoffs.

`nauro graph` renders the store to one self-contained HTML file and opens it: a node-link map of every decision as the default view, plus drawn supersession lineage, a timeline, and a category browser. The demo store's consolidation, three retired decisions converging on the one that replaced them, draws as a fan. By default the file carries the full decision store, including each decision's body rendered as structured detail in the side panel, and lands in the store directory rather than your repo; `--no-include-bodies` produces a redacted titles-and-metadata artifact for wider sharing.

`nauro doctor` checks the store for structural defects: unparseable decision files, dangling or cyclic supersession refs, and status contradictions. It is deterministic and report-only — it never edits the store and always exits 0.

For real-project setup (`nauro init` / `nauro adopt`), cross-surface access, MCP tool reference, and architecture details, see the [main project README](https://github.com/nauro-ai/nauro#readme). Don't run `nauro setup` from `/tmp/nauro-demo`; that would wire the throwaway demo into your MCP client.

`nauro adopt --with-subagents` additionally installs Nauro's bundled Claude Code workflow subagents (`@nauro-planner`, `@nauro-executor`, `@nauro-reviewer`, `@nauro-tech-lead`) into `~/.claude/agents/`. Off by default to avoid overwriting locally-customized files; pass `--force-overwrite` to replace customized files.

## Why Nauro?

Nauro supports a human-ratified project-judgment loop. It captures what you decided and what you ruled out, with the reasoning, then brings related judgment into agent work. Keyword search over the decision store is one mechanism for putting prior reasoning in front of an agent at proposal time.

No model judges your decisions. The check uses deterministic keyword retrieval (BM25), is advisory, and never blocks a change. Agents draft additions, updates, and supersessions; you explicitly approve each one before `propose_decision` commits it in one call.

`check_decision` returns the related prior decisions (the `related_decisions` list shown above) so the agent can weigh them before proposing; Nauro ranks by keyword relevance and does not judge the proposal. On the approved `propose_decision` call, near-matches surface as advisory `similar_decisions`, and a clean proposal commits in one call. What you approve in one tool, every connected agent inherits; for example, a decision recorded in Claude Code is available later in Perplexity. The store is plain markdown in a folder you own. Run it fully locally with no account; cloud sync is opt-in.

## Hosted allowance

Nauro includes unlimited local usage, unlimited projects, and 5,000 remote MCP calls per month. For higher hosted limits, contact [thomas@nauro.ai](mailto:thomas@nauro.ai). See [nauro.ai/pricing](https://nauro.ai/pricing) for current details.

---

Apache 2.0 license. Part of the [nauro-ai/nauro](https://github.com/nauro-ai/nauro) monorepo.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.

<!-- mcp-name: ai.nauro/nauro -->
