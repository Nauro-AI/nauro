# Nauro

**Give your agents the context code leaves out.**

Nauro keeps current state, open questions, and human-approved project judgment in one record, ready for every agent you connect.

That record combines project scope, current state, and open questions with human-approved project judgment, including intent, goals, decisions, rationale, tradeoffs, and rejected paths.

Project judgment is the human-ratified part of the record. Context is the slice Nauro gives an agent for the work in front of it. Before connected agents plan or change work, Nauro surfaces that context. New or revised judgment becomes project truth only after you approve it.

The record belongs to the project and is available from Claude, Cursor, Codex, Perplexity, and other MCP clients.

[![PyPI](https://img.shields.io/pypi/v/nauro.svg)](https://pypi.org/project/nauro/) [![Python](https://img.shields.io/pypi/pyversions/nauro.svg)](https://pypi.org/project/nauro/) [![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Status:** Stable (1.x). Semantic versioning covers the CLI, local stdio MCP contract, on-disk store format, and curated `nauro-core` import API. Cloud sync and hosted MCP are versioned separately.

## Pareto example

https://github.com/user-attachments/assets/9e6c475b-c584-470b-84c2-12f01b3a425a

*A real Codex session in Pareto, a reproducible mock project. Nauro retrieves the existing concurrency cap before the agent recommends a top-tier override. After approval, the agent records the decision and changes the code. This is one controlled example; recurring value in other projects remains unproven.*

## Install

```bash
uv tool install nauro
```

`uv` fetches its own Python, so Python does not need to be pre-installed. Install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS or Linux, or follow the [PowerShell instructions](https://docs.astral.sh/uv/getting-started/installation/) on Windows. If Python 3.10 or newer is already installed, `pipx install nauro` and `pip install nauro` also work.

The first interactive run asks once about anonymous telemetry and defaults to no. `nauro telemetry status` shows the setting, and `NAURO_TELEMETRY=0` disables telemetry and the prompt.

## Try it in 30 seconds

The Pennykeep demo needs no account, MCP wiring, or agent restart:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Store dollar amounts as decimal numbers"
```

Pennykeep is a fictional local-first budgeting project with thirteen decisions. Its top match is:

```text
D001 "Amounts stored in integer cents, never floating point"
```

The stored decision says to use integer cents because binary floating point cannot represent values such as 0.10 exactly. The agent reads the decision body, including its rationale and rejected path, and judges its relevance.

`check-decision` uses deterministic BM25 keyword retrieval and ranks results by keyword overlap rather than semantic meaning. It never blocks a change. The MCP `check_decision` tool returns the same result directly to a connected agent before planning.

## Use Nauro on a real repo

Run adoption from the root of an existing Git repository:

```bash
cd your-repo
nauro adopt
```

The command registers the project, writes `.nauro/config.json`, and wires local MCP access plus the adoption skill for Claude Code, Cursor, and Codex. Restart the agent, then invoke `/nauro-adopt`.

The skill reads project documentation for rationale and inspects the repository as supporting evidence. When the files show what the project does but not why, it asks you instead of inventing a decision.

Review `.nauro/config.json` and commit it with the repo. Setup variants and optional tooling are covered in the [quickstart](https://nauro.ai/docs/quickstart) and [agents and skills guide](https://nauro.ai/docs/agents-and-skills).

## How the loop works

1. Nauro orients the agent with project scope, current state, open questions, and relevant prior judgment.
2. You and the agent clarify missing intent, constraints, or tradeoffs.
3. If the work needs new or revised judgment, the agent drafts it and waits for your explicit approval.
4. The agent plans, recommends, or implements with that context in view.
5. The agent explains how the context shaped the result. You can accept or correct it, grant an exception, or reopen and supersede prior judgment.
6. The agent reports meaningful completed progress as current state, so later connected agents inherit the updated state and approved judgment.

`check_decision` surfaces related records before a technical choice. `propose_decision` commits only the draft you have approved.

No model decides what your project believes. Retrieval remains advisory, and agent output cannot silently change project judgment. Current state and open questions share the record, but they do not use the judgment approval contract. The local record is plain Markdown in a folder you own, and cloud sync is optional.

## When it fits

If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, Nauro may be more than you need.

Nauro is intended for longer-lived work where context can be lost between sessions or needs to move between tools and repositories. If agents reliably consult committed decision files, those files may be enough. Built-in memories remain tied to one product; Nauro keeps the project record independent of the connected client.

Current limits:

- The project record is necessarily partial. Nauro can surface only what has been recorded. Retrieval does not establish that an agent understands the project or that a decision still applies unchanged.
- Keyword retrieval can miss a relevant decision phrased in different language. An optional embeddings index adds synonym matching.
- Each connected-agent check adds an MCP read and remains advisory. Nauro does not force an agent to honor a result.
- Nauro is not a backlog, task orchestrator, code indexer, or replacement for Git history.
- Hosted projects are single-owner today. Nauro does not provide team governance or concurrent multi-writer authority.

## Local and cloud access

Local use needs no account or cloud service. The record stays as plain Markdown on your machine unless you enable cloud sync. The hosted connector at `https://mcp.nauro.ai/mcp` and cloud setup are documented in [Connect your agent](https://nauro.ai/docs/connect).

Codex remote users must set `mcp_oauth_callback_port = 8765` at the top of `~/.codex/config.toml`. Without it, Codex selects a random callback port that Auth0 cannot accept.

## Inspect the record

`nauro graph` writes one self-contained HTML file with Graph, Lineage, Timeline, and Browse views. By default, it embeds decision titles, metadata, open-question summaries, and full decision bodies. With `--no-include-bodies`, it keeps the titles, metadata, and question summaries but omits full decision bodies.

`nauro doctor` reports structural defects without editing the store.

Connected agents can request L0 for a concise orientation, L1 for a bounded working set, or L2 for a full dump. On mature stores, L2 can reach hundreds of thousands of tokens, so agents should start with L0 and retrieve exact records as needed.

## Documentation

- [Quickstart](https://nauro.ai/docs/quickstart): installation, demo, adoption, imports, and setup variants
- [Connect your agent](https://nauro.ai/docs/connect): local MCP, remote MCP, chat surfaces, and cloud sync
- [Agents and skills](https://nauro.ai/docs/agents-and-skills): optional workflow tooling, including `nauro setup all --with-subagents`
- [Core concepts](https://nauro.ai/docs/concepts) and [store guide](https://nauro.ai/docs/store): judgment, context, retrieval, and files
- [CLI reference](https://nauro.ai/docs/cli) and [MCP reference](https://nauro.ai/docs/mcp): commands, tools, schemas, and examples
- [Data storage](https://nauro.ai/docs/data-storage): local and hosted storage boundaries

## Uninstall

From an adopted repo:

```bash
nauro adopt --remove
```

This reverses adoption for the current repo. The Markdown store stays on disk unless you explicitly pass `--purge-store` for the project's last repo. Remove the CLI with `uv tool uninstall nauro`.

## Development

The monorepo contains the `nauro` CLI and the reusable `nauro-core` library. See [CLAUDE.md](CLAUDE.md) for the architecture.

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q
```

Report bugs and request features in [GitHub Issues](https://github.com/Nauro-AI/nauro/issues).

Apache 2.0.

Named for Peter Naur's *Programming as Theory Building* (1985).
