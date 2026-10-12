# CLAUDE.md — Nauro

Nauro is a local CLI + MCP server that maintains versioned project context for AI coding agents across repos and tools.

## The one architectural fact that matters

The project store lives at `~/.nauro/projects/<project-id>/` — **not** inside any repo. This is the core design decision. A per-repo store would break cross-repo context, which is the problem Nauro exists to solve. The registry at `~/.nauro/registry.json` is keyed by project id (ULID); each entry carries the project name as metadata plus one or more associated repo paths on the machine.

```
~/.nauro/
  registry.json                  # id-keyed entries: name, mode, repo paths
  projects/
    <project-id>/
      project.md                 # stable: goals, non-goals, users, constraints
      state_current.md           # volatile: current sprint, blockers, recent completions
      state_history.md           # append-only history of completed work
      stack.md                   # tech choices with rationale and rejected alternatives
      open-questions.md          # append-only unresolved threads
      decisions/
        001-title.md             # one file per decision, sequential, immutable
      snapshots/
        v001.json                # full store capture at a point in time
```

All files are freeform markdown. No database. No JSON for content — JSON only for `registry.json` and snapshots.

## Stack

- CLI: Python 3.10+, Typer
- MCP server: local stdio transport (FastMCP), spawned by the MCP client
- Storage: flat markdown + JSON snapshots
- Templating: f-strings and Python string templates — no Jinja2

## CLI commands

Principal commands (run `nauro --help` for the full surface):

- `nauro init <name>` — register a new project in `~/.nauro/`, scaffold the store, associate repo paths
- `nauro adopt` — adopt an existing repo: register it, wire MCP across surfaces, install the `/nauro-adopt` skill (`--with-skills` adds the four opt-in skills; `--with-subagents` adds the five bundled subagents)
- `nauro attach <project_id>` — associate the current repo with an existing cloud project (`--generation` installs an initial generation replica)
- `nauro reconnect` — connect or recover the project already named by this repository's `.nauro/config.json`
- `nauro link` — promote a local-only project to cloud
- `nauro note <text>` — append a decision (default) or question (if text ends with `?` or `--question` flag)
- `nauro sync` — capture a snapshot, regenerate `AGENTS.md` in all associated repos; a generation replica instead refreshes its installed generation from the server and never pushes local files (`--push-only` is refused)
- `nauro log` — list recent snapshots with metadata; on a generation replica only `--decisions` serves, from the installed generation, since snapshots stay on the server
- `nauro journal` — emit the project's write-path provenance journal as a JSON array on stdout
- `nauro status` — capability table for the current project (active surfaces, absolute store path); a generation replica reports its refresh state instead (generation, last refresh attempt, last successful refresh)
- `nauro doctor` — report deterministic store-integrity defects (unparseable decision files, dangling or cyclic supersession refs, status contradictions, duplicate decision numbers, duplicate active titles) plus repairable supersede backref orphans; report-only, exits 0 whether or not it finds defects; a generation replica is diagnosed through its installed generation and exits 1 only when that generation cannot be read
- `nauro repair` — flip the single unambiguous supersede backref orphan after interactive confirmation; every other shape is reported with guidance and left alone; a generation replica refuses the local repair, and `--judgment --project <project_id>` inspects or recovers a hosted judgment instead (`--saga`, `--resume`, `--abandon`, `--action`, `--resend-action`)
- `nauro graph` — render the decision graph to one self-contained HTML file in the store directory (the Nauro home for a generation replica) and open it
- `nauro share-context <slug> <content> --pointer-kind <brief|resume|selection> --summary <text>` — publish an immutable brief and discovery pointer; writes only through an attached generation replica
- `nauro update-stack <content>` — replace the stack document; writes only through an attached generation replica
- `nauro serve` — start the local MCP server (stdio transport)
- `nauro import --memory-bank <path>` — migrate a Cline/Roo Code Memory Bank
- `nauro import --adr <path>` — migrate Architecture Decision Records

Command groups: `setup <claude-code|cursor|codex|all>`, `auth <login|status|logout|refresh>`, `config <get|list|unset>`, `telemetry <status|enable|disable|reset>` (inert compatibility commands), `validate <status>`, `projects <list|rm>`, `questions <migrate>`, `hook <user-prompt-submit|codex-bootstrap>`.

Ten shared read/write MCP tools are mirrored as CLI commands, auto-generated from the tool allowlist in `cli/autogen.py` (underscored tool names become hyphenated commands). For example: `nauro check-decision`, `nauro diff-since-last-session [--days N]`, and `nauro propose-decision <rationale> [--title TITLE] [--operation add|update|supersede] [--rejected JSON] [--files-affected PATH ...]` (single-call commit on Tier 1 clean; Tier 2 BM25 hits surface as advisory `similar_decisions` on the same response). `--title` is optional: `operation="update"` appends rationale only, so a rationale-only update omits it.

`list[str]` flags (`--files-affected`, `--resolves-questions`) repeat: `--files-affected a.py --files-affected b.py`. `list[dict]` flags (`--rejected`) take a single JSON value: inline (`'[{...}]'`), `@file.json`, or `-` to read from stdin.

`share-context` and `update-stack` are registered explicitly in `cli/main.py`, not autogenerated. Both take `--request-mode submit|discover|recover|retry` with `--operation-id` and `--payload-digest` for recover and retry (reference modes retain the saved payload and identity); `update-stack` adds `--expected-revision`.

## MCP tools (12 local: 7 read, 5 write; 14 hosted)

`nauro_core.mcp_tools` holds 11 shared specs (`ALL_TOOLS`: 8 read, 3 write). `HOSTED_TOOLS` adds `update_stack`, `share_context` and `create_project` for the hosted HTTP server (14). The local stdio server registers 12: the shared tools minus `list_projects`, plus `update_stack` and `share_context`, which write only through an attached generation replica. `list_projects` and `create_project` are hosted-only; local installs auto-resolve to the single project store.

Read:
- `get_context(project, level)` — L0 concise summary, L1 working set, L2 full dump
- `get_raw_file(project, path)` — raw content of any store file
- `list_decisions(project, limit, include_superseded)` — browse decision history
- `get_decision(project, number)` — full content of a specific decision
- `diff_since_last_session(project, days)` — what changed since last session
- `search_decisions(project, query, limit)` — keyword search across decisions
- `check_decision(proposed_approach)`: surface related decisions without writing
- `list_projects()` — list user's projects (hosted-only)

Write:
- `propose_decision(project, title, rationale, ...)` — record a decision (single-call commit on Tier 1 clean)
- `flag_question(project, question, context)` — flag an open question
- `update_state(project, delta)` — report what was completed
- `share_context(slug, content, pointer_kind, summary, ...)` — publish an immutable brief and discovery pointer on a generation replica
- `update_stack(content, expected_revision, ...)` — replace the stack document on a generation replica; an omitted revision uses the installed replica revision
- `create_project(name, operation_id)` — create an empty hosted project (hosted-only)

## AGENTS.md

Generated by `nauro sync` into each associated repo root. Contains the L0 payload plus MCP connection instructions. Never hand-edited — a `# Manual` section at the bottom is preserved across regenerations. This is the cross-tool distribution layer for tools without MCP support.

## Conventions

- No Jinja2 — f-strings and string templates only
- All store I/O goes through `nauro.store.filesystem_store.FilesystemStore` implementing the `nauro_core.operations.Store` protocol; helpers in `reader.py` and `snapshot.py` support reads and snapshot capture respectively
- Tests: pytest with `tmp_path` fixture to avoid touching real `~/.nauro/`
- Linting: ruff
- `NAURO_HOME` env var overrides `~/.nauro/` for testing

## Project layout

```
src/nauro/
  agents/                # bundled subagent bodies + per-surface renderer
  auth.py                # Auth0 defaults, token load, single-flight refresh, 401 retry
  cli/
    main.py              # Typer app entry point
    commands/            # one module per command
    generation_reads.py  # route reads and sync through an installed generation replica
    generation_writes.py # typed generation writes through the standard CLI commands
  mcp/
    stdio_server.py      # stdio MCP server (FastMCP)
    tools.py             # canonical transport-agnostic tool adapters
    payloads.py          # L0/L1/L2 payload builders
  store/
    filesystem_store.py  # Store-protocol implementation backed by ~/.nauro/projects/
    reader.py            # read helpers
    snapshot.py          # capture, list, load snapshots
    registry.py          # ~/.nauro/registry.json CRUD + resolve_v2_from_path()
    repo_config.py       # per-repo .nauro/config.json read/write
    resolution.py        # project resolution from CWD
    config.py            # ~/.nauro/config.json read/write
    validator.py         # structural checks on store contents
  sync/
    generation_renewal.py     # bounded automatic credential acquisition before generation work
    generation_credentials.py # account credentials for projects on generation authority
    generation_session.py     # credentials for one resolved generation replica operation
  setup/                 # Typer-free wiring helpers: git hygiene, setup outcomes, render, CLAUDE.md bridge
  skills/                # bundled skill bodies
  templates/
    scaffolds.py         # nauro init template strings
    agents_md.py         # AGENTS.md generation
tests/
  fixtures/              # pre-scaffolded stores
```
