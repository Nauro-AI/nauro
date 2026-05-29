---
name: nauro-adopt
description: Seeds Nauro's project store from an existing repo. Use after `nauro adopt` has run locally. On filesystem-capable surfaces, reads docs (README, manifests, ADRs, Memory-Bank) for rationale and inspects code, config, tests, lockfiles, and recent git history for evidence, then surfaces targeted probes that turn evidence into rationale. On chat surfaces, operates on pasted content against an already-adopted project.
---

# Nauro adopt skill

The agent helps the user seed Nauro with context from the current repo. Before this skill runs, the user has run `nauro adopt` from the repo root, which created the project, wired MCP across surfaces, and installed this skill into the agent's surface directory. The agent's job here is to seed the Nauro store via MCP write tools: docs supply the rationale for documented decisions, code and config and tests and manifests and recent git history supply evidence, and the user supplies the "why" via targeted probes when only evidence is present. Do not invent rationale. Record only what was actually decided, with the reasoning that supports it.

## Surface modes

The agent's behaviour depends on whether the surface can read the repo directly.

- **Filesystem-capable surfaces** (Claude Code, Cursor, Codex CLI). The agent runs Steps 1–11 in full. Docs are read for rationale in Step 3; code, config, tests, manifests, and recent git history are inspected for evidence in Step 4; targeted probes in Step 6b turn evidence into rationale by asking the user.
- **Chat surfaces** (Claude.ai, ChatGPT, Perplexity). The agent has no shell. It operates only on content the user pastes into the chat (Step 3b), and only against an already-adopted project (verified in Step 2). The code-evidence path (Step 4) and the Step 6b probes are unavailable; the agent does not ask the user to paste code in lieu of running shell commands. The skill skips from Step 3b directly to Step 5.

## Step 1 — Detect repo root

The agent runs `git rev-parse --show-toplevel` from the current working directory. On failure: abort with "nauro adopt requires a git repository. Run 'git init' first, then re-run 'nauro adopt'."

## Step 2 — Already-adopted guard

The agent reads `<repo>/.nauro/config.json`. If it exists and parses as JSON: extract `id` and `name` and use them as the project handle.

If the file is missing, try two fallbacks before aborting:

1. **Worktree fallback.** Compare `git rev-parse --git-dir` and `git rev-parse --git-common-dir`. If they differ, the current checkout is a linked worktree, and `.nauro/` may only exist in the main worktree (common when a workspace tool gitignores `.nauro/` per-checkout). The main worktree path is the parent directory of `git rev-parse --path-format=absolute --git-common-dir`. Re-read `<main-worktree>/.nauro/config.json` there.
2. **Registry fallback.** Read `~/.nauro/registry.json`. Schema v2 keys each project by its id under `projects` — the dict key **is** the project `id`; the entry stores `name`, `mode`, `repo_paths`, etc. If any entry's `repo_paths` contains the current repo root or the resolved main-worktree path, use that dict key as the `id` and the entry's `name` as the project handle.

Abort only when both fallbacks miss, with: "This repo is not adopted yet. Run 'nauro adopt' from the repo root, restart this agent, then invoke /nauro-adopt again."

Pass `id` as the `project_id` argument on every subsequent MCP call (`propose_decision`, `update_state`, `flag_question`, `get_context`, `list_decisions`, `check_decision`, `get_decision`). Do not omit `project_id` even though the tool descriptions say it's optional — auto-resolve routes to the user's default project, which is **not** the project this skill is seeding when both local-mode and cloud-mode projects coexist.

## Step 3 — Read documentation

Docs are the rationale source. The agent reads the first match found per category, with the manifest workspace exception below. Files larger than 256KB are flagged to the user before reading. If the README category yields no match, surface "No README found; reading manifest only." to the user once and continue — manifest-only repos are still adoptable.

- **README**: `README.md`, `README.rst`, `README` (first found)
- **Manifest**: `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`, `pom.xml`, `build.gradle`, `composer.json`, `requirements*.txt`. Read the root manifest. If it declares a workspace, also read each member manifest. Workspace declarations to recognise:
    - `pyproject.toml` → `[tool.uv.workspace]` `members = [...]`
    - `package.json` (npm / Yarn classic) → top-level `workspaces` array
    - `pnpm-workspace.yaml` → top-level `packages:` list (pnpm does **not** use `package.json` `workspaces`)
    - `Cargo.toml` → `[workspace]` `members = [...]`

  Member entries are usually globs (`packages/*`, `crates/*`). Expand each glob and read every matched directory's manifest of the same ecosystem. Otherwise stop at the root match. The root manifest of a workspace is often a thin shim with no real dependencies; member manifests carry the actual stack.
- **Top-level docs**: `CONTRIBUTING.md`, `ARCHITECTURE.md`, `DESIGN.md`, `CLAUDE.md`, `AGENTS.md`
- **ADR directory**: `docs/adr/`, `docs/decisions/`, `architecture/decisions/`, `adr/` — every `.md` except templates and index files (`0000-template.md`, `README.md`)
- **Memory Bank**: `.context/`, `memory-bank/`, `cline_docs/` — `projectBrief.md`, `activeContext.md`, `techContext.md`, `decisionLog.md`, `progress.md`

### Step 3b — Chat-surface paste branch

When filesystem read is unavailable (chat surfaces), the agent first verifies the project exists by asking:

> Has this repo been adopted via `nauro adopt` locally? Tell me the project name and id, or paste `<repo>/.nauro/config.json`.

If the user cannot confirm an adopted project, the agent surfaces "Run `nauro adopt` locally first, then return here" and stops. Once the project is confirmed, the agent prompts for source content:

> This skill needs source content. Paste the contents of any of: README, manifest (pyproject.toml/package.json/etc.), ADRs, Memory-Bank files. Send each as a separate message labelled with the filename.

Chat-paste mode does not create projects, and chat surfaces skip Step 4 — code evidence is shell-bound and the agent does not ask the user to paste code in lieu of running commands.

## Step 4 — Read code evidence

Filesystem-capable surfaces only. After Step 3, the agent inspects repo state to identify architectural facts that docs may not name. Each finding is held as an **observation** keyed by file path — never proposed as a decision on its own. Observations drive Step 6b's targeted probes; the user (or a Step 3 doc) must supply the rationale before anything flows into the decision log.

Categories to inspect (the goal is enough evidence to ask the right probes, not a full inventory):

- **Source-tree layout.** `ls` the repo root and any workspace member directories. Note monorepo vs single-package shape, `src/`-layout vs flat, service-per-directory vs library-per-package.
- **CI / CD config.** `.github/workflows/`, `.gitlab-ci.yml`, `.circleci/config.yml`, `azure-pipelines.yml`. Note the version matrix (which Python / Node / Rust versions are tested), required checks, and deployment targets.
- **Test layout.** Test directory location (`tests/`, `__tests__/`, `*_test.go`), framework signals (pytest / jest / cargo test / go test), fixture conventions, and any integration vs unit split.
- **Lint and format config.** `ruff.toml`, `.eslintrc*`, `.prettierrc*`, `mypy.ini`, `pyrightconfig.json`, `rustfmt.toml`, `.golangci.yml`.
- **Infrastructure as code.** `Dockerfile`, `docker-compose.yml`, `terraform/`, `cdk.json`, `serverless.yml`, `sam/template.yaml`, `pulumi/`.
- **Lockfiles and pinned versions.** Presence of `poetry.lock`, `uv.lock`, `package-lock.json`, `pnpm-lock.yaml`, `Cargo.lock`, `go.sum`. Note which package manager is canonical; do not enumerate every dependency.
- **Recent git history.** `git log --oneline -n 30 --no-merges` to see what's active, when activity slowed, and which areas of the tree are touched most.

Step 4 ends with a list of observations, not a list of candidate decisions. Promotion into a candidate happens in Step 6b, after the user answers a probe.

## Step 5 — Call get_context

The agent calls `get_context` (MCP) to surface what the scaffold already wrote — `001-initial-setup.md` and bracketed-prompt placeholders in `project.md` / `stack.md`. The agent uses this to (a) avoid duplicating the scaffold's first decision, (b) confirm the project resolved correctly, and (c) learn what `check_decision` will treat as the existing baseline in Step 7.

## Step 6 — Triage candidates

Every candidate the agent surfaces lands in one of five buckets: **documented decision** (Step 6a), **code-evidenced fact needing rationale** (Step 6b, filesystem-capable only), **stack inventory** (Step 6c), **open question** (Step 9), or **ignore** (Step 10 refusal contract). The agent walks 6a → 6b → 6c in order, surfacing one list per substep and waiting for user input before continuing.

### Step 6a — Documented decisions

Candidates where a Step 3 source explicitly states acceptance and rationale (and, when applicable, rejected alternatives). Each entry: number, title (≤60 chars), one-line summary (≤140 chars), source location. A candidate also qualifies as documented when the rule is stated in one explicit source document and the rationale is stated in a *different* explicit source document — cite both source locations in the entry (e.g. "rule: CLAUDE.md §Conventions; rationale: README §How it works"). Code is never the rationale half of a cross-source pair; if rationale is absent from every doc the candidate falls to 6b or 6c. The agent prints the full list as one message:

> Reply 'keep N' / 'edit N: <new title>' / 'skip N' per item. Or 'keep all' to accept everything. Reply 'done' when finished.

### Step 6b — Code-evidenced facts needing rationale

Filesystem-capable surfaces only. For each Step 4 observation that points at a real architectural choice (workspace shape, framework pick, test-layout convention, CI matrix, IaC target, …), the agent emits one targeted probe per observation, using this template verbatim:

> I see X in file/path; was Y considered; what pushed you toward X?

Substitute `X` with the observed fact, `file/path` with the source it came from, and `Y` with a credible alternative the agent inferred (or "an alternative" if none is obvious). Probes are batched into one message; the user replies per item:

> Reply 'rationale N: <why>' to record it as a decision (it flows into the Step 7 write loop), 'inventory N' to drop it to stack inventory (6c), 'question N' to flag it as an open question (Step 9), or 'skip N' to drop it entirely.

When the user supplies rationale, the candidate is promoted into the Step 7 write loop with `operation="add"` by default — Step 7 step 3 reclassifies to `update` or `supersede` if `check_decision` surfaces an overlap.

### Step 6c — Stack inventory

Languages, frameworks, package managers, license, lint tooling, and similar facts visible in the manifest or a "Stack" section without verbatim rationale. Stack inventory is folded into Step 8's `update_state` delta as a short summary — never written as decisions on its own. The only exception is a stack choice promoted into 6a via cross-doc stitching (rule in manifest, rationale in a separate doc). The agent prints the inventory list as one message:

> The agent will fold these into the project state summary in Step 8. Reply 'drop N' to remove any, 'edit N: <new wording>' to rewrite, or 'done' to accept.

## Step 7 — Write loop

For each kept candidate from 6a and each rationale-supplied 6b answer, the agent runs the full propose protocol:

1. Call `check_decision(proposed_approach=<title or short description>, project_id=...)`. `check_decision` returns related decisions via BM25 retrieval and a deterministic assessment. It does NOT judge conflicts.
2. When the response lists related decisions, call `get_decision` on each before proposing — `mode=header` to triage, `mode=full` for those you reason about; the assessment doesn't judge. Call signature: `get_decision(number=N, project_id=...)`.
3. Classify the operation:
    - **add** (default; new ground, no existing decision covers it).
    - **update** when the candidate augments an existing decision's rationale only. The server consumes only `rationale` on update; `title`, `confidence`, `decision_type`, `reversibility`, `files_affected`, and `rejected` are rejected at the boundary — use supersede if any of those must change.
    - **supersede** when the title or other metadata must change, or the candidate replaces or contradicts an existing decision. Pass the full new body and set `affected_decision_id`.
    - **skip** when the candidate is a duplicate (e.g. matches `001-initial-setup` or a candidate already seeded earlier in this same adopt run).
4. Call `propose_decision` using the call signature for the chosen operation — these are operation-specific by design so an agent copying the template cannot accidentally send a field the server will reject:
    - For **add** (new decisions):
      ```
      propose_decision(project_id=..., title=..., rationale=..., operation="add", rejected=..., confidence=...)
      ```
    - For **update** (rationale-only — the server rejects every other field at the boundary):
      ```
      propose_decision(project_id=..., rationale=..., operation="update", affected_decision_id=...)
      ```
    - For **supersede** (replace a decision or change metadata; pass the full new body):
      ```
      propose_decision(project_id=..., title=..., rationale=..., operation="supersede", affected_decision_id=..., rejected=..., confidence=...)
      ```

   `rationale` is drawn from explicit Step 3 source text (6a) or the user's probe answer (6b). `confidence` defaults to `medium`; use `high` only when a source explicitly says "accepted" or "approved". Include rejected alternatives only when the source names them or the user supplies them in the probe answer.
5. After `propose_decision`, the kernel commits immediately on Tier 1 clean. If `similar_decisions` is non-empty, surface those hits to the user before drafting the next candidate; the human approval gate is the chat-session moment before this call, not a second tool call after it.

One `propose_decision` per candidate. No batching across candidates without surfacing each `similar_decisions` response first.

## Step 8 — State composition (one update_state call)

The agent reads `activeContext.md` body and `progress.md` items, then composes one delta:

```
{activeContext_body_with_leading_h1_stripped}

## Recently completed
- {progress_item_1}
- {progress_item_2}
- ...
```

If Step 6c produced inventory items, append a short trailing section:

```

## Stack
- {inventory_item_1}
- {inventory_item_2}
- ...
```

If only one of activeContext / progress / inventory is present, the corresponding sections are omitted. If none is present, `update_state` is skipped entirely. **The agent does not call `update_state` per progress item or per inventory entry** — `update_state` archives prior state to history, and the L0 payload ignores history.

## Step 9 — Flag open questions

Sources: explicit `## Open Questions` sections, lines beginning `Q:` or `TODO:` in CONTRIBUTING / ARCHITECTURE docs, user-noted gaps from 6a / 6b / 6c dialogue ("we never decided X"), and Step 6b probes the user routed to `question N` or answered with "I don't know". Per candidate: surface, ask `keep / edit / skip`. On keep → `flag_question(question, context, project_id=...)`.

## Step 10 — Refusal contract

The agent records facts that source documents explicitly state, or that the user supplies in response to a Step 6b probe. The agent does not infer rationale from prose, does not invent rejected alternatives, does not assume `confidence=high` from tone, does not summarize a paragraph into a "decision" the source never called a decision. Five specific traps to avoid:

- **Demo prose.** Decision-shaped statements inside README quickstart examples, illustrative scenarios, hypothetical user prompts, comparison tables, or product-demo narratives are false positives unless an independent source (CLAUDE.md, ADR, ARCHITECTURE.md, manifest, Memory-Bank) names the same fact as an actual project decision. In that case the independent source is the citation, not the demo prose.
- **Stack inventory is not decisions.** Languages, frameworks, package managers, license, and lint tooling listed in a manifest or a "Stack" section are inventory (6c) — they go into the Step 8 delta, not the decision log. Cross-doc stitching is the only path that promotes inventory into 6a.
- **Cross-doc stitching is not inference.** When 6a stitches a rule from one source with rationale from another, both source locations must be explicit and both must be docs. The agent never fills in a missing rationale itself, never treats two restatements of the same rule as "rule + rationale", and never uses code as the rationale half.
- **Code evidence is not rationale.** Source code, config files, tests, IaC templates, lockfiles, and git history are evidence of *what* — they tell you a stack was picked, a pattern is present, a CI matrix exists. They do not state *why*. The agent does not propose decisions from code alone; it asks a Step 6b probe and only records the decision once the user (or a Step 3 doc) supplies rationale.
- **No code-only dependency decisions.** Dependency choices visible in `pyproject.toml`, `package.json`, lockfiles, or similar manifests never become decisions on their own. They land in 6c (stack inventory, folded into Step 8 state) or surface as 6b probes when a particular pick looks load-bearing enough to warrant a rationale.

Boundary cases go to 6b (probe → opt-in rationale) or 6c (inventory roll-up). They never flow into 6a without an explicit source.

## Step 11 — Summary

The agent prints one block:

- Project: `<name>` (id: `<pid>`, store: `<store-path>`)
- Sources read: `<comma-separated list of docs from Step 3>`
- Code evidence inspected: `<comma-separated list of Step 4 categories, or "skipped — chat surface">`
- Code-evidence probes asked: `P` / answered: `A`
- Decisions created: `N` (titles)
- Decisions skipped: `M` (titles)
- State updated: `yes`/`no`
- Open questions flagged: `K`
- Next: run `nauro sync` from the repo to capture a snapshot.
