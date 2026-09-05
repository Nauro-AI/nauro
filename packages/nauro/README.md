# Nauro

What every agent should know

Keep your project's direction in human hands as agents do more of the work.

Nauro keeps a living project record. It combines project scope, current state, and open questions with human-approved project judgment: intent, goals, decisions, rationale, tradeoffs, and rejected paths. Project judgment is the human-ratified part of the record; context is the relevant slice of the record an agent receives for the work in front of it. Works with Claude, Perplexity, Cursor, Codex, and any MCP client.

## How the loop works

1. Nauro orients the agent with project scope, current state, open questions, and relevant prior judgment.
2. You and the agent clarify missing intent, constraints, or tradeoffs.
3. If the work needs new or revised judgment, the agent drafts it and waits for your explicit approval.
4. The agent plans, recommends, or implements with that context in view.
5. The agent explains how the context shaped the result, and you accept, correct, except, reopen, or supersede it in conversation.
6. The agent reports meaningful completed progress as current state, so later connected agents inherit the updated state and approved judgment.

The markdown store, context summaries, BM25 retrieval, advisory checks, and optional sync support this loop. They do not replace your judgment or silently change project truth.

## Why project history matters

Pennykeep is the fictional budgeting app bundled with Nauro. Suppose an agent is asked to simplify onboarding by replacing envelope budgeting with a passive spending tracker and charts.

The recorded decision, **Envelope budgeting method**, explains the tradeoff. In the fictional tester history, people who only saw past spending kept overspending. Assigning income before spending helped them change their behavior. The team accepted more setup effort to support that goal.

With that context, an agent can suggest simpler envelope setup or ask you whether to revisit the budgeting model. This is an illustrative outcome, not a recorded agent response.

## Install

```bash
uv tool install nauro
```

No `uv`? Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or the [PowerShell line](https://docs.astral.sh/uv/getting-started/installation/) on Windows. Already on Python 3.10+? `pipx install nauro` (or `pip install nauro`) works too.

## Quickstart

Try the Pennykeep example without an account or agent setup. On macOS or Linux:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Replace envelope budgeting with a passive spending tracker and charts"
```

Look for **Envelope budgeting method** in the related decisions. Its rationale explains why the team accepted more onboarding effort to help users plan spending. The command retrieves prior decisions; it does not run an agent or change the recorded decision.

See the [demo guide](https://nauro.ai/docs/quickstart#demo) for PowerShell commands and more ways to explore the sample.

If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, Nauro may be more than you need. Nauro is designed for context that must persist across longer histories, sessions, tools, repos, machines, or repeated handoffs.

`nauro graph` renders the store to one self-contained HTML file and opens it: a node-link map of every decision as the default view, plus drawn supersession lineage, a timeline, and a category browser. The demo store's consolidation, three retired decisions converging on the one that replaced them, draws as a fan. By default the file carries the full decision store, including each decision's body rendered as structured detail in the side panel, and lands in the store directory rather than your repo; `--no-include-bodies` produces a redacted titles-and-metadata artifact for wider sharing.

`nauro doctor` checks the store for structural defects: unparseable decision files, dangling or cyclic supersession refs, and status contradictions. It is deterministic and report-only — it never edits the store and always exits 0. It also names one repairable defect separately: a supersession recorded on the newer decision but never written back to the older one.

`nauro repair` is the only command that acts on what doctor names, and it acts on that one shape. When a single decision in the store claims to supersede another that is still active with no reference back, it shows you both decisions with their versions and dates, states the exact field change, and asks. Anything less clear-cut — two decisions claiming the same predecessor, a cycle, a file that will not parse — is reported with guidance and left alone. Nothing is written without your answer, and there is no flag to skip the question.

For real-project setup (`nauro init` / `nauro adopt`), cross-surface access, MCP tool reference, and architecture details, see the [main project README](https://github.com/nauro-ai/nauro#readme). Don't run `nauro setup` from `/tmp/nauro-demo`; that would wire the throwaway demo into your MCP client.

For cross-surface onboarding, run `nauro adopt --with-skills --with-subagents`. Plain adopt installs `nauro-adopt`. `--with-skills` adds `nauro-ship-task`, `nauro-context`, `nauro-loop`, and `nauro-interview`. `--with-subagents` adds Nauro's planner, executor, reviewer, and tech-lead agents for Claude Code, Cursor, and Codex. Cursor stores its native project agents under `.cursor/agents/`.

Cursor runs `nauro-ship-task` natively. `nauro-loop` Program Delivery stays on hold.

Restart, then seed the store with `/nauro-adopt` in Claude Code, `$nauro-adopt` in Codex, or `@nauro-adopt` in Cursor Agent chat. On a new machine, run `nauro setup cursor`, then restart Cursor. Commit `.nauro/config.json`, `.cursor/rules/nauro-*.mdc`, and `.cursor/agents/nauro-*.md`, not the gitignored, machine-local `.cursor/mcp.json`.

Cursor Cloud Agents need separate MCP configuration at `cursor.com/agents`. If you use Nauro's hosted connector there, link and sync the project first.

Re-running onboarding refreshes Nauro-owned workflow files and saves differing copies as backups. It leaves third-party skills and agents untouched. Pass `--force-overwrite` only when you do not want backups.

## Why Nauro?

Nauro supports a human-ratified project-judgment loop. It captures what you decided and what you ruled out, with the reasoning, then brings related judgment into agent work. Keyword search over the decision store is one mechanism for putting prior reasoning in front of an agent at proposal time.

No model judges your decisions. The check uses deterministic keyword retrieval (BM25), is advisory, and never blocks a change. Agents draft additions, updates, and supersessions; you explicitly approve each one before `propose_decision` commits it in one call.

`check_decision` returns the related prior decisions (the `related_decisions` list in the response) so the agent can weigh them before proposing; Nauro ranks by keyword relevance and does not judge the proposal. On the approved `propose_decision` call, near-matches surface as advisory `similar_decisions`, and a clean proposal commits in one call. What you approve in one tool, every connected agent inherits; for example, a decision recorded in Claude Code is available later in Perplexity. The store is plain markdown in a folder you own. Run it fully locally with no account; cloud sync is opt-in.

## Hosted allowance

Nauro includes unlimited local usage, unlimited projects, and 5,000 remote MCP calls per month. For higher hosted limits, contact [thomas@nauro.ai](mailto:thomas@nauro.ai). See [nauro.ai/pricing](https://nauro.ai/pricing) for current details.

---

Apache 2.0 license. Part of the [nauro-ai/nauro](https://github.com/nauro-ai/nauro) monorepo.

Named for Peter Naur, whose 1985 paper *Programming as Theory Building* argued the real program is the theory in the programmer's mind, not the code. Every fresh agent session is the equivalent of losing that programmer.

<!-- mcp-name: ai.nauro/nauro -->
