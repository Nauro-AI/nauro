# Nauro adopt prompt — chat surface (paste-content) variant

Use this prompt when adopting a Nauro project from a chat surface that has no
filesystem access (Claude.ai, ChatGPT, Perplexity). The chat agent reads
content **you paste into the chat** instead of reading repo files itself.

**Prerequisite — chat-paste mode does not create projects.** First run the CLI:

```sh
nauro adopt --name <your-project-name>
```

That registers the project, wires MCP across surfaces, and installs the
session + adopt skills. Once that has run, you can use the prompt below from
any connected chat surface to seed the project's store with context.

If you have `nauro` installed locally, `nauro adopt --print-prompt` outputs
this same body to stdout (without the intro paragraph above).

---

# Nauro adopt skill

The agent helps the user seed Nauro with context from the current repo. Before this skill runs, the user has run `nauro adopt` from the repo root, which created the project, wired MCP across surfaces, and installed this skill into the agent's surface directory. The agent's job here is to read the repo's documentation (README, manifests, ADRs, Memory-Bank) and seed the Nauro store via MCP write tools. The agent records facts that source documents explicitly state — it does not invent decisions from prose. On chat surfaces (Claude.ai, ChatGPT) without filesystem access, the agent uses paste-content mode (Step 3b); chat-paste mode requires the project to already exist (run `nauro adopt` locally first).

## Step 1 — Detect repo root

The agent runs `git rev-parse --show-toplevel` from the current working directory. On failure: abort with "nauro adopt requires a git repository. Run 'git init' first, then re-run 'nauro adopt'."

## Step 2 — Already-adopted guard

The agent reads `<repo>/.nauro/config.json`. If it exists and parses as JSON: extract `id` and `name` and use them as the project handle.

If the file is missing, try two fallbacks before aborting:

1. **Worktree fallback.** Compare `git rev-parse --git-dir` and `git rev-parse --git-common-dir`. If they differ, the current checkout is a linked worktree, and `.nauro/` may only exist in the main worktree (common when a workspace tool gitignores `.nauro/` per-checkout). Resolve the main worktree path from `git worktree list --porcelain` (the first `worktree` line) and re-read `<main-worktree>/.nauro/config.json`.
2. **Registry fallback.** Read `~/.nauro/registry.json`. If any project's `repo_paths` contains the current repo root or the resolved main-worktree path, use that project's `id` and `name`.

Abort only when both fallbacks miss, with: "This repo is not adopted yet. Run 'nauro adopt' from the repo root, restart this agent, then invoke /nauro-adopt again."

Pass `id` as the `project_id` argument on every subsequent MCP call (`propose_decision`, `confirm_decision`, `update_state`, `flag_question`, `get_context`, `list_decisions`). Do not omit `project_id` even though the tool descriptions say it's optional — auto-resolve routes to the user's default project, which is **not** the project this skill is seeding when both local-mode and cloud-mode projects coexist.

## Step 3 — Read source files

The agent reads the first match found per category, with the manifest workspace exception below. Files larger than 256KB are flagged to the user before reading. If the README category yields no match, surface "No README found; reading manifest only." to the user once and continue — manifest-only repos are still adoptable.

- **README**: `README.md`, `README.rst`, `README` (first found)
- **Manifest**: `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`, `pom.xml`, `build.gradle`, `composer.json`, `requirements*.txt`. Read the root manifest. If it declares a workspace — `[tool.uv.workspace]` with `members = [...]` in `pyproject.toml`, a top-level `workspaces` array in `package.json` (npm/pnpm/yarn), or `[workspace]` with `members = [...]` in `Cargo.toml` — also read each member manifest at its declared path. Otherwise stop at the root match. The root manifest of a workspace is often a thin shim with no real dependencies; member manifests carry the actual stack.
- **Top-level docs**: `CONTRIBUTING.md`, `ARCHITECTURE.md`, `DESIGN.md`, `CLAUDE.md`, `AGENTS.md`
- **ADR directory**: `docs/adr/`, `docs/decisions/`, `architecture/decisions/`, `adr/` — every `.md` except templates and index files (`0000-template.md`, `README.md`)
- **Memory Bank**: `.context/`, `memory-bank/`, `cline_docs/` — `projectBrief.md`, `activeContext.md`, `techContext.md`, `decisionLog.md`, `progress.md`

The agent does not read source code, tests, IaC templates, or git history during adopt. Those surfaces are out of scope on purpose — every recorded fact must trace back to an explicit source document so the refusal contract in Step 9 holds.

### Step 3b — Chat-surface paste branch

When filesystem read is unavailable (chat surfaces), the agent first verifies the project exists by asking:

> Has this repo been adopted via `nauro adopt` locally? Tell me the project name and id, or paste `<repo>/.nauro/config.json`.

If the user cannot confirm an adopted project, the agent surfaces "Run `nauro adopt` locally first, then return here" and stops. Once the project is confirmed, the agent prompts for source content:

> This skill needs source content. Paste the contents of any of: README, manifest (pyproject.toml/package.json/etc.), ADRs, Memory-Bank files. Send each as a separate message labelled with the filename.

Chat-paste mode does not create projects.

## Step 4 — Call get_context

The agent calls `get_context` (MCP) to surface what the scaffold already wrote — `001-initial-setup.md` and bracketed-prompt placeholders in `project.md` / `stack.md`. The agent uses this to (a) avoid duplicating the scaffold's first decision, (b) confirm the project resolved correctly.

## Step 5 — Build candidate list, surface FIRST

The agent triages the source content into two lists.

**Step 5a — Clear decisions.** Candidates where the source explicitly states acceptance and rationale (and, when applicable, rejected alternatives). Each entry: number, title (≤60 chars), one-line summary (≤140 chars), source location. A candidate also qualifies as a clear decision when the rule is stated in one explicit source document and the rationale is stated in a *different* explicit source document — cite both source locations in the entry (e.g. "rule: CLAUDE.md §Conventions; rationale: README §How it works"). The agent does not invent the second source to make a candidate fit; if rationale is absent from every source the candidate goes to Step 5b. The agent prints the full list as one message:

> Reply 'keep N' / 'edit N: <new title>' / 'skip N' per item. Or 'keep all' to accept everything. Reply 'done' when finished.

**Step 5b — Boundary candidates.** Facts that *might* be decisions but where the source does not state rationale verbatim. Stack inventory (languages, frameworks, package managers, license, lint tooling) lives here unless a source frames the choice with rationale. Surfaced after the user finishes Step 5a:

> The agent thinks these might be decisions but couldn't extract rationale verbatim from the source. Each is opt-in only — reply 'opt N: <rationale>' to record any of them with rationale you provide; otherwise they are skipped.

## Step 6 — Iterate kept items

For each kept item from 5a (and each opt-in from 5b):

1. Call `propose_decision(title=..., rationale=..., operation="add", rejected=..., confidence=...)`. Adopt seeds the store from source documents, so it normally uses `operation="add"` and does not set `affected_decision_id`. `rationale` is drawn from explicit source text or user-provided opt-ins. `confidence` defaults to `medium`; use `high` only when the source explicitly says "accepted" or "approved". Include rejected alternatives only when the source names them.
2. **If `propose_decision` returns no conflicts**: call `confirm_decision(confirm_id)` automatically — no further user prompt.
3. **If `propose_decision` returns conflicts**: surface the conflict text verbatim, ask `confirm-anyway / edit / skip`. On user 'confirm' → `confirm_decision(confirm_id)`.

One propose+confirm per decision. No batching.

## Step 7 — State composition (one update_state call)

The agent reads `activeContext.md` body and `progress.md` items, then composes one delta:

```
{activeContext_body_with_leading_h1_stripped}

## Recently completed
- {progress_item_1}
- {progress_item_2}
- ...
```

If only one source is present, the corresponding section is omitted. If neither is present, `update_state` is skipped entirely. **The agent does not call `update_state` per progress item** — `update_state` archives prior state to history, and the L0 payload ignores history.

## Step 8 — Flag open questions

Sources: explicit `## Open Questions` sections, lines beginning `Q:` or `TODO:` in CONTRIBUTING/ARCHITECTURE docs, user-noted gaps from Steps 5–6 ("we never decided X"). Per candidate: surface, ask `keep / edit / skip`. On keep → `flag_question(question, context)`.

## Step 9 — Refusal contract

The agent records facts that source documents explicitly state. The agent does not infer rationale from prose, does not invent rejected alternatives, does not assume `confidence=high` from tone, does not summarize a paragraph into a "decision" the source never called a decision. Three specific traps to avoid:

- **Demo prose.** Decision-shaped statements inside README quickstart examples, illustrative scenarios, hypothetical user prompts, comparison tables, or product-demo narratives are false positives unless an independent source (CLAUDE.md, ADR, ARCHITECTURE.md, manifest, Memory-Bank) names the same fact as an actual project decision. In that case the independent source is the citation, not the demo prose.
- **Stack inventory.** Languages, frameworks, package managers, license, and lint tooling listed in a manifest or a "Stack" section are inventory, not decisions, unless the source frames the choice with rationale (in which case Step 5a applies, possibly via cross-doc stitching).
- **Cross-doc stitching is not inference.** When Step 5a stitches a rule from one source with rationale from another, both source locations must be explicit. The agent never fills in a missing rationale itself, and never treats two restatements of the same rule as "rule + rationale".

Boundary cases go to Step 5b (opt-in only — write rationale yourself), not into the clear-decisions list.

## Step 10 — Summary

The agent prints one block:

- Project: `<name>` (id: `<pid>`, store: `<store-path>`)
- Sources read: `<comma-separated list>`
- Decisions created: N (titles)
- Decisions skipped: M (titles)
- State updated: yes/no
- Open questions flagged: K
- Next: run `nauro sync` from the repo to capture a snapshot.
