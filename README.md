<p align="center">
  <img src="docs/images/nauro-wordmark-dark.svg" alt="Nauro" width="180">
</p>

<p align="center"><strong>Human Controlled Project Truth</strong></p>

<p align="center">
  <a href="https://pypi.org/project/nauro/"><img alt="PyPI" src="https://img.shields.io/pypi/v/nauro.svg"></a>
  <a href="https://github.com/Nauro-AI/nauro/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Nauro-AI/nauro/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
</p>

Keep your project's direction in human hands as agents do more of the work.

Before work, Nauro surfaces the relevant part of that record. Approved judgment and reported progress carry into later sessions and connected tools.

**Status:** Stable (1.x). Semantic versioning covers the CLI, local stdio MCP contract, on-disk store format, and curated `nauro-core` import API. Cloud sync and hosted MCP are versioned separately.

## See it in practice

https://github.com/user-attachments/assets/f75ede99-db11-4460-bc09-801c86df1e19

*A real Codex session, then a Claude session in Pareto, Nauro's development mock project.*

## Install

```bash
uv tool install nauro
```

Install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS or Linux, or use the [Windows instructions](https://docs.astral.sh/uv/getting-started/installation/). With Python 3.10 or newer, `pipx install nauro` also works.

## Try the demo

Pennykeep, the bundled demo, needs no account or agent setup:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Store dollar amounts as decimal numbers"
```

The top result is `D001`, **Amounts stored in integer cents, never floating point**. Its rationale explains why floating point makes money totals drift.

## Use it on your repo

```bash
cd your-repo
nauro adopt --with-skills --with-subagents
```

Restart, then seed the store with `/nauro-adopt` in Claude Code, `$nauro-adopt` in Codex, or `@nauro-adopt` in Cursor Agent chat.

Plain adopt installs `nauro-adopt`. `--with-skills` adds `nauro-ship-task`, `nauro-context`, `nauro-loop`, and `nauro-interview`. `--with-subagents` adds four workflow agents for Claude Code, Cursor, and Codex. Cursor stores its native project agents under `.cursor/agents/`.

Cursor runs `nauro-ship-task` natively. `nauro-loop` Program Delivery stays on hold.

On a new machine, run `nauro setup cursor`, then restart Cursor. Commit `.nauro/config.json`, `.cursor/rules/nauro-*.mdc`, and `.cursor/agents/nauro-*.md`, not the gitignored, machine-local `.cursor/mcp.json`. Cursor Cloud Agents need separate MCP configuration at `cursor.com/agents`.

Run `nauro status` to check MCP, skills, and workflow agents. Run `nauro doctor` to check the project store.

Nauro surfaces prior judgment for the agent to assess. Retrieval is advisory. New or revised judgment requires your explicit approval. Local use needs no account, and the record is plain Markdown on your machine. Cloud sync and remote MCP access are optional. Nauro sends no product analytics.

Nauro preserves an existing `AGENTS.md` unless you explicitly run `nauro sync`. A `# Manual` section survives replacement.

## Desktop app (macOS)

A free, read-only viewer for the project record: timeline, map, list, activity, and docs. First launch installs the CLI, connects your agents, and adopts or attaches a project.

https://github.com/user-attachments/assets/e23d3d7f-2d5b-4ce4-be18-fcaff74e7973

[Download for macOS (Apple silicon)](https://github.com/Nauro-AI/nauro-app-releases/releases/latest/download/Nauro-macOS-arm64.dmg), signed and notarized.

## Fit

If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, Nauro may be more than you need.

Nauro is intended for longer-lived work where context can decay across sessions, tools, repositories, machines, or repeated handoffs.

Read the [documentation](https://nauro.ai/docs) for setup variants, concepts, command references, storage, and cloud access.

## Development

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q
```

Report bugs and request features in [GitHub Issues](https://github.com/Nauro-AI/nauro/issues).

Apache 2.0. Named for Peter Naur's *Programming as Theory Building* (1985).
