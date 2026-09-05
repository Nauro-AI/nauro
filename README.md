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

Nauro records the project decisions you approve, including their reasons and rejected alternatives. It brings those reasons into later agent sessions.

**Status:** Stable (1.x).

## See it in practice

https://github.com/user-attachments/assets/f75ede99-db11-4460-bc09-801c86df1e19

*A Codex session, then a Claude session in Pareto, Nauro's development mock project.*

## Is Nauro a fit?

Nauro supports longer-lived work across sessions, tools, and handoffs. If a small repo plus a reliable AGENTS.md or CLAUDE.md keeps agents oriented, Nauro may be more than you need.

Retrieval is advisory. New or revised decisions require your explicit approval. Local use needs no account; the record is plain Markdown on your machine. Cloud sync is optional. Nauro sends no product analytics.

## Get started

### Desktop app (macOS)

The free app installs the CLI, connects agents, and sets up a project. Its read-only viewer shows the project record.

[Download for macOS (Apple silicon)](https://github.com/Nauro-AI/nauro-app-releases/releases/latest/download/Nauro-macOS-arm64.dmg), signed and notarized. [Setup guide](https://nauro.ai/docs/desktop-app).

<details>
<summary>Watch the desktop app tour</summary>

https://github.com/user-attachments/assets/e23d3d7f-2d5b-4ce4-be18-fcaff74e7973

</details>

### CLI

```bash
uv tool install nauro
```

[Install uv](https://docs.astral.sh/uv/getting-started/installation/), or use `pipx install nauro` with Python 3.10 or newer.

## Try the demo (optional)

Pennykeep is the bundled fictional budgeting app.

- **Request:** Replace envelope budgeting with spending charts to simplify onboarding.
- **Prior decision:** In the fictional tester history, passive tracking did not help users control spending. The team accepted extra setup so users assign income before spending.
- **Possible plan:** Simplify envelope setup, or ask whether to revisit the budgeting model.

No account or agent setup required. On macOS or Linux:

```bash
mkdir -p /tmp/nauro-demo && cd /tmp/nauro-demo
nauro init --demo
nauro check-decision "Replace envelope budgeting with a passive spending tracker and charts"
```

Look for **Envelope budgeting method** in the related decisions. This retrieves prior reasoning; it does not run an agent or change decisions. [PowerShell and demo guide](https://nauro.ai/docs/quickstart#demo).

## Use it on your repo

For CLI setup:

```bash
cd your-repo
nauro adopt --with-skills --with-subagents
```

Skip this command if the desktop wizard already adopted your repository. Restart your agent, then run `/nauro-adopt` in Claude Code, `$nauro-adopt` in Codex, or `@nauro-adopt` in Cursor. Review the proposed project record.

Approve one existing decision you want future sessions to remember. Start a fresh session and ask about a task where it matters. Check how the agent uses the reason.

Run `nauro status` to check the connection. See [agent workflows](https://nauro.ai/docs/agents-and-skills) and [setup details](https://nauro.ai/docs/quickstart).

An existing `AGENTS.md` is preserved unless you run `nauro sync`. Its `# Manual` section survives replacement.

## Documentation

[Documentation](https://nauro.ai/docs) covers commands, storage, and cloud access. Semantic versioning covers the CLI, local stdio MCP contract, store format, and curated `nauro-core` API. Cloud sync and hosted MCP are versioned separately.

## Development

```bash
uv sync --all-packages --all-extras
uv run pytest packages/nauro-core/tests/ -x -q
uv run pytest packages/nauro/tests/ -x -q
```

Report bugs and request features in [GitHub Issues](https://github.com/Nauro-AI/nauro/issues).

Apache 2.0. Named for Peter Naur's *Programming as Theory Building* (1985).
