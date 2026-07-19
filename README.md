# Nauro

**Give your agents the context code leaves out.**

Nauro keeps current state, open questions, and human-approved project judgment in one record, ready for every agent you connect.

The record combines project scope, state, open questions, and the rationale behind decisions. Nauro surfaces the relevant slice before work, then carries approved judgment and reported progress into later sessions and connected tools.

[![PyPI](https://img.shields.io/pypi/v/nauro.svg)](https://pypi.org/project/nauro/) [![Python](https://img.shields.io/pypi/pyversions/nauro.svg)](https://pypi.org/project/nauro/) [![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Status:** Stable (1.x). Semantic versioning covers the CLI, local stdio MCP contract, on-disk store format, and curated `nauro-core` import API. Cloud sync and hosted MCP are versioned separately.

## See it in practice

https://github.com/user-attachments/assets/9e6c475b-c584-470b-84c2-12f01b3a425a

*A real Codex session in Pareto, a reproducible mock project. Nauro retrieves an existing concurrency cap before the agent recommends an override. After approval, the agent records the decision and changes the code. This is one controlled example. Recurring value in other projects remains unproven.*

## Install

```bash
uv tool install nauro
```

Install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS or Linux, or use the [Windows instructions](https://docs.astral.sh/uv/getting-started/installation/). With Python 3.10 or newer, `pipx install nauro` also works.

## Try Pennykeep

The local demo needs no account or agent setup:

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

Restart your agent, then invoke `/nauro-adopt` in Claude Code or `$nauro-adopt` in Codex. The skill reads project documentation and code, asks you about missing rationale, and seeds the project record without inventing decisions. The same onboarding command also installs the gated `nauro-ship-task` workflow and its planner, executor, reviewer, and tech-lead agents for both surfaces.

Run `nauro status` to verify MCP plus the installed skills and workflow agents. Run `nauro doctor` to check the project store itself. Re-running the onboarding command refreshes Nauro-owned workflow files and keeps recoverable backups of differing copies. It never migrates or changes third-party skills.

Nauro surfaces prior judgment for the agent to assess. Retrieval is advisory and never blocks a change. New or revised judgment is written only after your explicit approval. Local use needs no account. The local record is plain Markdown that you own. Cloud sync and remote MCP access are optional. Nauro sends no product analytics.

Adoption, setup, and incidental regeneration preserve an unmanaged `AGENTS.md` and warn. `nauro sync` is the sole explicit overwrite path. A `# Manual` section survives replacement.

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
