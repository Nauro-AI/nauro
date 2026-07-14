# Nauro

**Give your agents the context code leaves out.**

Nauro keeps current state, open questions, and human-approved project judgment in one record, ready for every agent you connect.

Nauro keeps a living project record. It combines project scope, current state, and open questions with human-approved project judgment: intent, goals, decisions, rationale, tradeoffs, and rejected paths. Project judgment is the human-ratified part of the record; context is the relevant slice of the record an agent receives for the work in front of it.

Before connected agents plan or change work, Nauro surfaces the relevant parts of that record. Afterward, they explain how the context shaped the result and report what changed. New or revised judgment becomes project truth only after you approve it.

It works across Claude, Cursor, Codex, Perplexity, and any MCP client. The same project record travels with the work, rather than belonging to a single tool or session.

[![PyPI](https://img.shields.io/pypi/v/nauro.svg)](https://pypi.org/project/nauro/) [![Python](https://img.shields.io/pypi/pyversions/nauro.svg)](https://pypi.org/project/nauro/) [![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

https://github.com/user-attachments/assets/9e6c475b-c584-470b-84c2-12f01b3a425a

*An agent checks the project's prior decisions before it plans, then records the approved decision and makes the change. Captured in Codex.*

**Status:** Stable (1.x). The nauro CLI, the stdio MCP tool contract, and the on-disk store format follow semantic versioning. CI covers both public packages on Python 3.10-3.14. Cloud sync is versioned and operated separately.

More at [nauro.ai](https://nauro.ai).

## How the loop works

1. Nauro orients the agent with project scope, current state, open questions, and relevant prior judgment.
2. You and the agent clarify missing intent, constraints, or tradeoffs.
3. If the work needs new or revised judgment, the agent drafts it and waits for your explicit approval.
4. The agent plans, recommends, or implements with that context in view.
5. The agent explains how the context shaped the result, and you accept, correct, except, reopen, or supersede it in conversation.
6. The agent reports meaningful completed progress as current state, so later connected agents inherit the updated state and approved judgment.

Nauro supports this loop with a plain markdown store, compact context, and deterministic retrieval. Decisions include the alternatives you ruled out and the reasoning behind them; the record also carries current state and open questions. When an agent proposes an approach, `check_decision` runs keyword retrieval (BM25) over those files and surfaces related records before the agent plans or writes code.

At session start, agents can read L0 for a concise orientation, use L1 for a bounded working set, or request L2 for a full dump. On mature stores, L2 can reach hundreds of thousands of tokens, so agents should start with L0 and pull exact decisions as needed.

No model judges your decisions. The check is advisory and never blocks a change. Agents draft decision additions, updates, and supersessions; you explicitly approve each one in the conversation before `propose_decision` commits it in one call. The store is a folder you own; remove Nauro and the markdown stays.

## Install

```bash
uv tool install nauro     # uv fetches its own Python — nothing else needed
```

No `uv`? Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or the [PowerShell line](https://docs.astral.sh/uv/getting-started/installation/) on Windows. Already on Python 3.10+? `pipx install nauro` (or `pip install nauro`) works too.

First run asks once about anonymous telemetry, defaulting to no; nothing is sent unless you opt in. `nauro telemetry status` shows the current setting, and `NAURO_TELEMETRY=0` disables both the telemetry and the prompt.

## Use it on your repo

```bash
cd your-repo
nauro adopt   # registers the project, wires MCP for Claude Code, Cursor, and Codex
```

Restart your agent and invoke `/nauro-adopt`: the installed skill seeds the store from your README, manifests, ADRs, and git history, and asks you for the reasoning it cannot cite. `nauro adopt` writes `.nauro/config.json` into the repo (commit it), and `nauro adopt --remove` undoes everything it wired. Just looking first? The demo below runs with no wiring and no restart.

## Try it in 30 seconds

No account, no MCP wiring, no restart:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Store dollar amounts as decimal numbers"
```

The demo store holds thirteen example decisions for a local-first budgeting app. One of them ruled out storing money as floating-point dollars in favor of integer cents, because binary floating point cannot hold a value like 0.10 exactly and totals drift by a penny, and `check-decision` surfaces it as the top match before your agent can re-propose it:

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
      "rationale_preview": "Every monetary amount (transactions, budgets, balances) is stored as an integer number of cents and formatted to dollars only for display. Binary floating point cannot represent most decimal amounts..."
    }
  ],
  "assessment": "Found 5 related decisions. Top match: D001 \"Amounts stored in integer cents, never floating point\" (status active, decided 2026-03-15, BM25 8.5). Ranked by keyword overlap, not meaning — judge relevance from the decision body, not the rank. Call get_decision on each related decision before proposing.",
  "project": { "id": "01K...", "name": "demo-project" }
}
```

Output abbreviated to the top match; the live call returns all five related decisions, ranked by score. The same result reaches your agent through the MCP `check_decision` tool, so it sees the prior decision in the flow rather than after the fact. To run the same check against your own decisions, jump back to [Use it on your repo](#use-it-on-your-repo).

`nauro graph` renders the decision history to one self-contained HTML file and opens it: a node-link map of the whole store, a lineage view per supersession thread, a timeline, and a category browser with superseded decisions dimmed. The file embeds the decision bodies by default, so it lands in the store directory rather than the repo; the flags for writing it elsewhere or redacting bodies are in the [CLI reference](https://nauro.ai/docs/cli).

<picture><source media="(prefers-color-scheme: dark)" srcset="docs/images/graph-dark.png"><img src="docs/images/graph-light.png" alt="The nauro graph default view of a project store: decisions as nodes colored by category across five clusters, with the largest node linked to the seven earlier decisions it retired, plus smaller supersession fans and two short supersession chains at the edges." width="900"></picture>

*A project store rendered by nauro graph: supersession threads converge on the decisions that replaced them, and standalone decisions cluster by category.*

`nauro doctor` checks the store for structural defects: unparseable decision files, supersession refs pointing at a decision that no longer exists or forming a cycle, and status contradictions such as an active decision that also records being superseded. It is deterministic and report-only — it never edits the store and always exits 0 — so it is safe to run any time you want to confirm the record is internally consistent.

## Why not ADRs, grep, CLAUDE.md, or built-in agent notes?

A committed decision log plus a reliable pointer in `AGENTS.md` or `CLAUDE.md` can be enough, especially in a small repo. Nauro is designed for longer histories that must stay available across sessions, tools, repos, machines, or repeated handoffs. It can surface related decisions through MCP when an agent proposes a change, while `nauro sync` regenerates a committable `AGENTS.md` summary for clones and tools without MCP wiring.

Against a coding tool's built-in memory (Claude Code memory, Cursor memories): those are scoped to one tool and one user. Nauro's record belongs to the project. The same store answers in Claude, Cursor, Codex, and any MCP client, across every repo you associate with it.

Against tools that extract notes from conversations automatically: Nauro records human-ratified judgment. An entry is a reviewed choice with its rationale and the alternatives you rejected. An agent drafts the change, you explicitly approve it, and `propose_decision` commits it in one call. Entries are retrieved by deterministic keyword search you can audit and superseded rather than silently rewritten. They remain plain markdown in a folder you own.

## When Nauro helps, and when it doesn't

Nauro is designed for long-lived projects where agents need project judgment before acting: architecture choices, rejected approaches, migration plans, operational constraints, and decisions that recur across sessions, tools, repos, machines, or handoffs.

If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, Nauro may be more than you need.

The limits are worth knowing. It surfaces only what has been recorded as a decision. It adds an MCP round-trip to the agent's flow. Retrieval is keyword-based, which is fast, offline, and auditable, and can miss a decision phrased in different words than the proposal; an optional embeddings index is available for closer synonym matching.

## Adoption paths and options

`nauro adopt` above covers the common case: one existing repo. The variants:

**Already keeping ADRs or a Memory Bank:**

```bash
nauro import --adr docs/adr          # NNN-title.md decision records
nauro import --memory-bank .context  # Cline / Roo Code Memory Bank
```

`nauro import` migrates the existing records into the current repo's project store (run `nauro init` or `nauro adopt` first) and captures a snapshot. Memory-Bank `decisionLog.md` entries need `## Decision: <title>` headings to import.

**New project:**

```bash
nauro init my-project
nauro setup claude-code   # or: nauro setup all
```

`nauro init` and `nauro attach` write `.nauro/config.json` and generate `AGENTS.md` immediately, including for `--demo` and `--add-repo`. If the repo already has a hand-authored `AGENTS.md`, Nauro warns and leaves it unchanged. Commit `.nauro/config.json`; review generated project context before deciding whether `AGENTS.md` belongs in the repository. For cloud sync from the start, run `nauro auth login` first, then `nauro init --cloud my-project`.

**One project across several repos:** the store lives outside any repo, so multiple repos can share it. Associate another repo with an existing project from inside that repo:

```bash
cd ../my-other-repo
nauro init my-project --add-repo .
```

Re-running plain `nauro init my-project` in a second repo creates a *separate* project that shares no decisions — use `--add-repo` to link them instead.

**Chat surfaces** (Claude.ai, Perplexity): run `nauro adopt` from a terminal first, then point the chat agent at [`docs/adopt-prompt.md`](docs/adopt-prompt.md).

**Optional: bundled subagents.** Add `--with-subagents` to `nauro adopt`, or run `nauro setup all --with-subagents`, to install Nauro's bundled Claude Code subagents into `~/.claude/agents/`. They are off by default; installed, a typical chain looks like:

- `@nauro-planner` before non-trivial work. Drafts a plan and classifies doctrine risk (GREEN/AMBER/RED) against your decision log.
- `@nauro-executor` after a plan is agreed. Implements it, runs tests, commits locally, and drafts the PR body. It does not push or open a PR.
- `@nauro-reviewer` before merging. Audits the diff for real bugs and for missing decision references.
- `@nauro-tech-lead` to set or correct direction. Reads the decision log, audits PRs against doctrine, and drafts any needed doctrine change for your approval.

**Optional: Codex lifecycle bootstrap.** Install project-scoped Codex hooks when you want Nauro's preflight and L0 context injected at session and subagent start:

```bash
nauro setup codex --with-hooks   # or: nauro setup all --with-hooks
```

Start a fresh Codex session, open `/hooks`, and trust the Nauro definitions once. The hooks live in `<repo>/.codex/hooks.json`, no-op outside adopted projects, and fail open when Nauro cannot be resolved. `nauro status` reports their on-disk wiring and executable health.

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
nauro propose-decision "In-memory cache for hot read paths" --title "Adopt Redis" \
    --files-affected src/cache.py --files-affected src/api.py \
    --rejected '[{"alternative": "Memcached", "reason": "Less feature-rich"}]'
```

Repeat `--files-affected` for each entry. `--rejected` accepts inline JSON, `@file.json`, or `-` to read from stdin.

## Uninstall

```bash
nauro adopt --remove   # from the repo root
```

`adopt --remove` is the inverse of `nauro adopt` for that repo: it removes the MCP, skill, subagent, and hook wiring across surfaces, strips the generated `AGENTS.md` (a hand-written `# Manual` section is preserved), deletes `.nauro/config.json`, and deregisters the repo, after one confirmation prompt (`--yes` skips it). When a project spans several repos, only the current repo is dropped and shared artifacts stay for the siblings.

The decision store is never deleted by default; it stays on disk as plain markdown. Pass `--purge-store` to delete it too, allowed only on the project's last repo. `nauro setup <surface> --remove` un-wires a single surface instead, and `uv tool uninstall nauro` (or `pipx uninstall nauro`) removes the CLI itself.

## Packages

| Package | Path | Install |
|---|---|---|
| `nauro` | `packages/nauro/` | `uv tool install nauro` |
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
