<p align="center">
  <img src="docs/images/nauro-wordmark-dark.svg" alt="Nauro" width="180">
</p>

<p align="center"><strong>What every agent should know</strong></p>

<p align="center">
  <a href="https://pypi.org/project/nauro/"><img alt="PyPI" src="https://img.shields.io/pypi/v/nauro.svg"></a>
  <a href="https://github.com/Nauro-AI/nauro/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Nauro-AI/nauro/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
</p>

Keep your project's direction in human hands as agents do more of the work.

Nauro records the project decisions you approve, including their reasons and rejected alternatives. It brings relevant decisions into later agent sessions so you do not have to explain them again. Project scope, current state, and open questions travel with that record.

**Status:** Stable (1.x).

## Why project history matters

Pennykeep, the fictional budgeting app bundled with Nauro, helps users assign income to category envelopes before they spend it.

Imagine asking an agent:

> Simplify onboarding by replacing envelope budgeting with a passive spending tracker and charts.

That could be a reasonable product change. But Pennykeep's recorded decision, **Envelope budgeting method**, explains why the team chose its current approach. In the fictional tester history, people who only saw past spending kept overspending. Assigning money before spending helped them change their behavior. The team accepted more setup effort to support that goal.

Nauro can surface that decision before the agent plans the change. An illustrative response would be:

> Pennykeep chose upfront allocation because passive tracking did not help its testers control spending. I suggest simplifying envelope setup while keeping allocation. Removing it would change the product's budgeting model and needs your approval.

The useful context is the project's reason for choosing an approach. A later session can use it, and you can still decide to change it.

## See it in practice

https://github.com/user-attachments/assets/f75ede99-db11-4460-bc09-801c86df1e19

*A real Codex session, then a Claude session in Pareto, Nauro's development mock project, showing project judgment across agent sessions.*

## Is Nauro a fit?

Nauro is intended for longer-lived work where context can decay across sessions, tools, repositories, machines, or repeated handoffs. If a small repo plus a reliable `AGENTS.md` or `CLAUDE.md` keeps agents oriented, Nauro may be more than you need.

Nauro surfaces prior judgment for the agent to assess. Retrieval is advisory. New or revised judgment requires your explicit approval. Local use needs no account, and the record is plain Markdown on your machine. Cloud sync and remote MCP access are optional. Nauro sends no product analytics.

## Get started

Choose the desktop app for guided setup on macOS, or install the CLI to work from your shell.

### Desktop app (macOS)

The free app walks you through installing the CLI, connecting your agents, and adopting or attaching a project. It also provides a read-only viewer for the project record: timeline, map, list, activity, and docs.

[Download for macOS (Apple silicon)](https://github.com/Nauro-AI/nauro-app-releases/releases/latest/download/Nauro-macOS-arm64.dmg), signed and notarized. See the [desktop app guide](https://nauro.ai/docs/desktop-app) for setup details.

<details>
<summary>Watch the desktop app tour</summary>

https://github.com/user-attachments/assets/e23d3d7f-2d5b-4ce4-be18-fcaff74e7973

</details>

### CLI

```bash
uv tool install nauro
```

Install `uv` with `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS or Linux, or use the [Windows instructions](https://docs.astral.sh/uv/getting-started/installation/). With Python 3.10 or newer, `pipx install nauro` also works.

## Try the demo (optional)

Try the Pennykeep example above without an account or agent setup. With the CLI installed, run these commands in a temporary directory on macOS or Linux:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Replace envelope budgeting with a passive spending tracker and charts"
```

Look for **Envelope budgeting method** in the related decisions. Its rationale explains the tradeoff between simpler onboarding and assigning money before spending. This command retrieves prior decisions; an agent uses that context to assess a plan. It does not run an agent or change the recorded decision.

See the [demo guide](https://nauro.ai/docs/quickstart#demo) for PowerShell commands and more ways to explore the sample. Keep agent setup in your own repository.

## Use it on your repo

For CLI setup, enter your repository and run:

```bash
cd your-repo
nauro adopt --with-skills --with-subagents
```

If the desktop wizard already adopted your repository, continue with the agent step below.

Restart your agent, then run `/nauro-adopt` in Claude Code, `$nauro-adopt` in Codex, or `@nauro-adopt` in Cursor Agent chat. The agent reviews the repository and helps you build its project record. Review proposed decisions and approve only those that reflect your intent.

For a first useful result, choose one existing decision you want future sessions to remember. After approving its record, start a fresh agent session and ask about a task where that decision matters. Check that the agent finds the reason and explains how it affects the approach.

Run `nauro status` to check the connection. See the [agent and skill guide](https://nauro.ai/docs/agents-and-skills) for optional workflows and the [setup guide](https://nauro.ai/docs/quickstart) for agent-specific configuration and troubleshooting.

Nauro preserves an existing `AGENTS.md` unless you explicitly run `nauro sync`. A `# Manual` section survives replacement.

## Documentation

Read the [documentation](https://nauro.ai/docs) for concepts, command references, storage, and cloud access.

Semantic versioning covers the CLI, local stdio MCP contract, on-disk store format, and curated `nauro-core` import API. Cloud sync and hosted MCP are versioned separately.

## Development

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q
```

Report bugs and request features in [GitHub Issues](https://github.com/Nauro-AI/nauro/issues).

Apache 2.0. Named for Peter Naur's *Programming as Theory Building* (1985).
