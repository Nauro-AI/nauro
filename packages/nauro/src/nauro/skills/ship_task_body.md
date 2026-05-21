<!-- Source template. The dogfood files under .claude/, .cursor/, .agents/
     are the rendered surface. Surface frontmatter is added by render_skill().
     This body has no protocol-fragment tokens — the chain it describes calls
     check_decision / propose_decision / confirm_decision by name, but the
     canonical claims in nauro_core.protocol live on the /nauro-adopt and
     MCP-instructions surfaces, not here. -->

# Nauro ship task skill

Orchestrate a non-trivial code change end-to-end using Nauro's bundled `@nauro-planner`, `@nauro-executor`, `@nauro-reviewer`, and `@nauro-tech-lead` subagents, with Nauro doctrine gates at every architectural choice.

Task: $ARGUMENTS

If $ARGUMENTS is empty, ask the user for a one-paragraph task description and wait for it before proceeding.

## Prerequisites

This skill invokes the bundled `@nauro-*` subagents by name. They install via `nauro adopt --with-subagents` (or `nauro setup all --with-subagents`). If they are missing, the chain cannot run — surface that to the user and stop. The personal-subagent path (`@planner` / `@executor` / `@reviewer` without the `nauro-` prefix) is not a substitute: the bundled subagents call Nauro's MCP tools by design, which is what makes the doctrine gates load-bearing.

## Execute the chain without waiting for the user to prompt each step

Pause only at the two explicit gates marked GATE below.

## Pre-step — Doctrine triage before the planner spins up

Before invoking `@nauro-planner`, the parent session confirms the planner will run `check_decision` first. The planner's contract already requires this, but the chain enforces it explicitly: if the planner returns a RED verdict (proposal directly contradicts an active decision) and drafts a supersede, the chain pauses before the executor sees anything. The user gates the supersede via `confirm_decision`; only on confirmation does the executor see the approved plan. This is the doctrine equivalent of GATE 1 firing early — a RED that the user does not resolve never reaches code.

### 1. Plan

Invoke the `@nauro-planner` subagent with the task description. The planner runs `check_decision` against the proposed approach, classifies as GREEN / AMBER / RED, reads related decision bodies via `get_decision`, investigates the code, and returns a plan in the PR-template shape (Why / Approach / What changes / What's deferred / Test plan), plus the verdict line and any decision number it drafted.

If the planner returns RED with a supersede draft, the chain pauses here. Surface the draft to the user. Only on `confirm_decision` (or an explicit "override RED on the cited decision, proceed") does the chain continue to the executor.

### 2. GATE — plan approval (always when `propose_decision` is in play)

The Nauro chain gates the plan whenever the planner indicates it will file a decision via `propose_decision` for any architectural choice, or whenever the plan records a supersede / update draft. This is stricter than the personal `/ship-task` skill: there is no "low-stakes auto-proceed" path when doctrine writes are pending. The user's judgment is the gate on what enters the decision log.

A change additionally always gates if any of the following apply:

- Touches authentication, credentials, secrets, tokens, signing, or encryption
- Changes data model, schema, storage format, or existing on-disk / database layout
- Adds, removes, or renames public surface (CLI commands, public functions, MCP tools, API endpoints, env vars, config keys)
- Changes the contract between `nauro` and `nauro-core`, or anything `mcp-server` consumes from `nauro-core`
- Adds, removes, or major-version bumps a dependency
- Records a Nauro decision with reversibility `hard` or `moderate`
- Surfaces 2-3 defensible alternatives where the choice is genuinely architectural

**When gating:** surface the plan, surface alternatives as alternatives, wait for explicit user approval. Do not proceed without it. Auto-mode and standing "keep moving" directives do not override this gate — it exists precisely for the cases where the user wants their judgment in.

**When auto-proceeding (no doctrine writes, no high-stakes triggers):** post a one-paragraph summary ("Planner proposes: `<X>`. Auto-proceeding to executor — no doctrine writes, no high-stakes triggers.") and continue. The user can interrupt at any time; if the classification feels wrong, they redirect and the gate fires.

### 3. Execute

Invoke the `@nauro-executor` subagent with the approved plan. Include any confirmed decision number from step 1 in the prompt. The executor implements the plan, commits locally, and does not push (per its contract). Any architectural choice the executor makes that the plan did not pre-record gets filed via `propose_decision` at the moment the choice lands in code — not deferred to the end of the chain.

### 4. Local review (no user pause)

Immediately after the executor returns, invoke the `@nauro-reviewer` subagent in Mode A on the local diff and the drafted PR description. Do not surface "ready to push" to the user before the reviewer has audited.

### 5. Iteration on block

If the reviewer returns BLOCK, hand the hard-rule failures back to the `@nauro-executor` for fix — scoped strictly to what was flagged, no scope expansion. Then re-invoke `@nauro-reviewer`. Cap at 2 fix iterations. If still blocked after 2, surface both verdicts and the unresolved findings to the user.

### 6. Doctrine pass

When the reviewer returns APPROVE or APPROVE WITH NITS, invoke `@nauro-tech-lead` in Mode C on the same local diff. The tech-lead reads decisions, scans the diff for architectural choices, and returns GREEN / AMBER / RED with any drafted supersede or update confirm_ids.

- **GREEN** — proceed to GATE.
- **AMBER** — surface the constraints to the user alongside the push gate; user decides whether to address before push or document as a follow-up.
- **RED** — pause. Either redirect the executor (fix the drift, then re-run reviewer + tech-lead) or confirm the drafted supersede first. Do not push past a RED tech-lead verdict without an explicit human override on the cited decision.

The reviewer's bug-finding pass and the tech-lead's doctrine pass are deliberately separate concerns — the reviewer can return APPROVE on a clean diff that the tech-lead later flags for drift.

### 7. GATE — push confirmation (user)

When the reviewer returns APPROVE / APPROVE WITH NITS and the tech-lead returns GREEN (or AMBER with surfaced constraints), surface:

- The drafted PR description (full text)
- A one-paragraph diff summary
- The reviewer's verdict and any nits
- The tech-lead's verdict and any AMBER constraints
- Any Nauro decisions filed during the chain (numbers + one-line rationale)
- "Push and open PR?"

Wait for explicit approval.

### 8. Push

On approval, push the branch and open the PR with the drafted description as the body (`gh pr create --body "..."`). Decisions filed during the chain go in the PR's Approach section by paraphrase, not by raw decision number — the public-repo convention is to describe the doctrine move ("the bundled-subagents pattern"), not cite internal D-numbers.

## Rules

- Push and `gh pr create` happen only with explicit user approval at GATE 7.
- `confirm_decision` happens only with explicit user approval — the planner / executor / tech-lead draft and `propose_decision`, never confirm. Parallel `propose_decision` is safe; parallel `confirm_decision` is not — confirm sequentially.
- If anything fails or surprises mid-chain (a tool errors, tests fail unexpectedly, a verdict is incoherent), stop and surface to the user rather than recovering silently.
- The chain is doctrine-aware end-to-end: every architectural choice flows through `check_decision` (at planning), `propose_decision` (when the choice lands), and `confirm_decision` (under user control). Skipping any of those silently is a chain failure, not a shortcut.
