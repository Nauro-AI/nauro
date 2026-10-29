# CLAUDE.md — Nauro (monorepo)

@AGENTS.md

Nauro is a versioned project context system for AI coding agents. This is a `uv` workspace monorepo with two packages:

| Package | Path | Python | Purpose |
|---|---|---|---|
| `nauro` | `packages/nauro/` | 3.10+ | CLI + local MCP server (stdio). Reads/writes `~/.nauro/projects/` |
| `nauro-core` | `packages/nauro-core/` | 3.10+ | Shared pure-Python logic: parsing, validation, context assembly, constants. No I/O; compute-only deps. |

Each package has its own `pyproject.toml` and test suite; `packages/nauro/` also carries a package-level `CLAUDE.md` (nauro-core does not). The remote MCP server (`mcp-server/`) lives in a separate private repository.

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
        v001.json                # canonical store files at a point in time
```

All files are freeform markdown. No database. No JSON for content — JSON only for `registry.json` and snapshots.

## Cross-package architecture

```
~/.nauro/projects/<id>/          Local store (flat markdown + JSON snapshots)
        │
        ├── nauro CLI              reads/writes directly
        ├── local MCP (stdio)      reads/writes directly, spawned by Claude Code
        │
        └── nauro sync ──────►  hosted storage (a legacy cloud project pushes; a generation
                                     │           replica refreshes its installed generation instead)
                                     └── remote MCP (hosted HTTP server, separate repo) reads/writes it
```

## Config and credentials

User config lives at `~/.nauro/config.json` (written by `nauro auth login` and other feature-specific commands; inspect with `nauro config get/list/unset`, which resolve top-level keys only):
- `auth` object — credentials from `nauro auth login`, nested under one top-level `auth` key (so inspect with `nauro config get auth`, not a dotted path): `access_token` is the Auth0 bearer sent to the presign sync endpoints; `refresh_token` mints a fresh access token when the bearer expires; `sub` is the raw JWT subject id (identity; the block also persists `sanitized_sub` and `user_id`)
- Auth0 domain, client ID, API URL, and audience ship as defaults in `nauro/auth.py`; env vars (`NAURO_AUTH0_*`, `NAURO_API_URL`) or config keys override (domain + client_id must be set as a pair)
- A project on generation authority renews its credentials automatically before authenticated generation work (`sync/generation_renewal.py`, `sync/generation_credentials.py`, `sync/generation_session.py`); `nauro auth refresh` renews explicitly
- `NAURO_HOME` env var overrides `~/.nauro/` for testing

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

Command groups: `nauro setup <claude-code|cursor|codex|all>`, `nauro auth <login|status|logout|refresh>`, `nauro config <get|list|unset>`, `nauro telemetry <status|enable|disable|reset>` (inert compatibility commands), `nauro validate <status>`, `nauro projects <list|rm>`, `nauro questions <migrate>`, `nauro hook <user-prompt-submit|codex-bootstrap>`.

The 10 read/write MCP tools are also mirrored as CLI commands — `nauro check-decision`, `nauro propose-decision`, `nauro get-context`, `nauro diff-since-last-session [--days N]`, … — auto-generated from the tool allowlist in `cli/autogen.py` (underscored tool names become hyphenated commands). `share-context` and `update-stack` are registered explicitly in `cli/main.py`, not autogenerated. Both take `--request-mode submit|discover|recover|retry` with `--operation-id` and `--payload-digest` for recover and retry (reference modes retain the saved payload and identity); `update-stack` adds `--expected-revision`.

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

## Running tests

```bash
# nauro-core
uv run pytest packages/nauro-core/tests/ -x -q

# nauro
uv run pytest packages/nauro/tests/ -x -q

# Lint
uv run ruff check packages/ benchmarks/ scripts/
uv run ruff format --check packages/ benchmarks/ scripts/
uv run python scripts/check_ruff_budget.py
```

### Hosted-consumer symbols

The nauro-core names listed in `packages/nauro-core/tests/hosted_consumer_symbols.txt` are imported directly by the private hosted MCP server, well past the curated `nauro_core.__all__`. `test_hosted_consumer_contract.py` freezes each one's shape, so renaming, moving, or re-signing any of them is a cross-repo change: coordinate the mcp-server update in the same window. Regenerate the manifest with `python scripts/hosted_consumer_symbols.py <mcp-server>/src` after the consumer's imports change, and the snapshot with `NAURO_UPDATE_CONTRACT=1 .venv/bin/python -m pytest packages/nauro-core/tests/test_hosted_consumer_contract.py`.

## Code bar

Nauro holds a high code bar. These apply to every change and are enforced in review:

- Parse untrusted or semi-structured input (hook configs, registry JSON, MCP payloads, tool configs) into validated models at the boundary — Pydantic is already a nauro-core dependency. Downstream logic operates on typed objects, never raw dicts.
- `isinstance`/`.get()` chains, regex extraction of structure, and nested conditional loops are tripwires: model the data instead. Python is the wrong place for heavy looping over loosely-shaped data.
- Functions have limited scope: one job, named for it. Reach for classes and inheritance when the domain calls for them, not by default.
- Modules organize code accurately: shared pure logic lives in its own module (see `store/resolution.py`, `cli/_codex_hooks.py`), never as private helpers imported across command modules.
- Errors are typed; no sentinel strings for control flow.
- A multi-round fix cycle owes a design-coherent end state before merge: the result must read as designed, not patched.
- Code prose has a budget: 3 lines for a function or method docstring, 5 for a class, 15 for a module, and the module docstring is the only place in code a design note belongs. Code carries no history: no dates, PR or issue references, prior breakages, rejected alternatives, or audit findings; that material belongs in the commit message, the PR body, and the decision store. A comment exists only where the code cannot say it, and a docstring longer than its function body is a restructure signal, so split, name, or type the function instead. `scripts/check_docstring_budget.py` enforces this in CI against `scripts/docstring_budget_baseline.txt`, a checked-in list of pre-existing overruns that may only shrink. Ruff rules listed as ratcheted in the root `pyproject.toml` are gated the same way by `scripts/check_ruff_budget.py` against `scripts/ruff_budget_baseline.jsonl`; regenerate it with `--write-baseline` only when a count drops.

## Conventions

- No Jinja2 — f-strings and string templates only
- All store I/O goes through `nauro.store.filesystem_store.FilesystemStore` implementing the `nauro_core.operations.Store` protocol; helpers in `reader.py` and `snapshot.py` support reads and snapshot capture respectively
- Tests: pytest with `tmp_path` fixture to avoid touching real `~/.nauro/`
- Linting: ruff
- `NAURO_HOME` env var overrides `~/.nauro/` for testing
- MCP tool implementations in `packages/nauro/src/nauro/mcp/tools.py` are canonical — the stdio transport delegates to them
- `nauro-core` does no I/O; its runtime dependencies are compute-only (BM25, pydantic, PyYAML). Embeddings ship as an optional extra (`nauro-core[embeddings]`)

## Project layout

```
packages/nauro/
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

packages/nauro-core/
  src/nauro_core/
    __init__.py            # public API
    constants.py           # shared constants
    context.py             # context assembly
    decision_model.py      # Decision model + parse/format + protocol regexes
    parsing.py             # markdown/decision parsing
    validation.py          # structural validation
  tests/
```
