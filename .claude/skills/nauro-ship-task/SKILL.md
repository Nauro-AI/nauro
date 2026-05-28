---
name: nauro-ship-task
description: Run the full planner -> executor -> reviewer -> tech-lead -> user-confirm -> push chain for a non-trivial code change against Nauro's bundled @nauro-* subagents. Gates on the user whenever the planner or executor will file a Nauro decision; runs @nauro-tech-lead Mode C between reviewer-APPROVE and the push gate to catch doctrine drift the reviewer missed. Invoke explicitly with /nauro-ship-task <description>. Requires `nauro adopt --with-subagents` to have run.
---

# Nauro ship task skill

Orchestrate a non-trivial code change end-to-end using Nauro's bundled `@nauro-planner`, `@nauro-executor`, `@nauro-reviewer`, and `@nauro-tech-lead` subagents, with Nauro doctrine gates at every architectural choice.

Take the user's task description from the prompt that invoked this skill. If they did not include one, ask for a one-paragraph description and wait for it before proceeding.

## Prerequisites

This skill invokes the bundled `@nauro-*` subagents by name. They install via `nauro adopt --with-subagents` (or `nauro setup all --with-subagents`). If they are missing, the chain cannot run — surface that to the user and stop. The personal-subagent path (`@planner` / `@executor` / `@reviewer` without the `nauro-` prefix) is not a substitute: the bundled subagents call Nauro's MCP tools by design, which is what makes the doctrine gates load-bearing.

## Execute the chain without waiting for the user to prompt each step

Pause only at the two explicit gates marked GATE below.

## Pre-step — Doctrine triage before the planner spins up

Before invoking `@nauro-planner`, the parent session confirms the planner will run `check_decision` first. The planner's contract already requires this, but the chain enforces it explicitly: if the planner returns a RED verdict (proposal directly contradicts an active decision) and drafts a supersede, the chain pauses before the executor sees anything. The user approves the supersede in chat; only after explicit approval does the planner file via `propose_decision` (which commits immediately) and the executor see the approved plan. This is the doctrine equivalent of GATE 1 firing early — a RED that the user does not resolve never reaches code.

### 1. Plan

Invoke the `@nauro-planner` subagent with the task description. The planner runs `check_decision` against the proposed approach, classifies as GREEN / AMBER / RED, reads related decision bodies via `get_decision`, investigates the code, and returns a plan in the PR-template shape (Why / Approach / What changes / What's deferred / Test plan), plus the verdict line and any decision number it drafted.

If the planner returns RED with a supersede draft, the chain pauses here. Surface the draft to the user. Only on explicit user approval (or an explicit "override RED on the cited decision, proceed") does the chain continue to the executor.

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

Invoke the `@nauro-executor` subagent with the approved plan. Include any confirmed decision number from step 1 in the prompt. The executor implements the plan, commits locally, and does not push (per its contract). If the executor hits an architectural choice the plan did not pre-record, it does not file the decision itself — it surfaces the choice and its rationale in its handoff so the parent can gate it with the user (at the push gate, step 7) and route the filing to whoever owns it. A subagent has no user channel mid-run, and `propose_decision` commits on Tier 1 clean, so an executor filing inline would install binding doctrine with no human gate.

### 4. Local review (no user pause)

Immediately after the executor returns, invoke the `@nauro-reviewer` subagent in Mode A on the local diff and the drafted PR description. Do not surface "ready to push" to the user before the reviewer has audited.

### 5. Iteration on block

If the reviewer returns BLOCK, hand the hard-rule failures back to the `@nauro-executor` for fix — scoped strictly to what was flagged, no scope expansion. Then re-invoke `@nauro-reviewer`. Cap at 2 fix iterations. If still blocked after 2, surface both verdicts and the unresolved findings to the user.

### 6. Doctrine pass

When the reviewer returns APPROVE or APPROVE WITH NITS, invoke `@nauro-tech-lead` in Mode C on the same local diff. The tech-lead reads decisions, scans the diff for architectural choices, and returns GREEN / AMBER / RED with any drafted supersede / update awaiting user approval.

- **GREEN** — proceed to GATE.
- **AMBER** — surface the constraints to the user alongside the push gate; user decides whether to address before push or document as a follow-up.
- **RED** — pause. Either redirect the executor (fix the drift, then re-run reviewer + tech-lead) or confirm the drafted supersede first. Do not push past a RED tech-lead verdict without an explicit human override on the cited decision.

The reviewer's bug-finding pass and the tech-lead's doctrine pass are deliberately separate concerns — the reviewer can return APPROVE on a clean diff that the tech-lead later flags for drift.

### 7. GATE — push confirmation (user)

When the reviewer returns APPROVE / APPROVE WITH NITS and the tech-lead returns GREEN (or AMBER with surfaced constraints), surface — in this order — before asking for the push:

1. **A general summary of what's about to ship.** One short paragraph naming the branch, the commit hash, the file count + line delta, and the test result. Reviewer's verdict and any nits. Tech-lead's verdict and any AMBER constraints. Any Nauro decisions filed during the chain (numbers + one-line rationale each).
2. **The drafted PR description (full body, verbatim).** Paste the executor's PR body inside a fenced block exactly as it will land on GitHub — do not abbreviate, do not paraphrase, do not link to "the executor's output above". The user reads this body to decide whether to push; if it's not in front of them, the gate isn't really firing.
3. **Then ask: "Push and open PR?"** Wait for explicit approval. No push without an explicit "yes" / "go" / "push" reply.

If the body would be long, that's fine — paste it anyway. Skipping the verbatim body is a chain failure.

### 8. Push

On approval, push the branch and open the PR with the drafted description as the body (`gh pr create --body "..."`). Decisions filed during the chain go in the PR's Approach section by paraphrase, not by raw decision number — the public-repo convention is to describe the doctrine move ("the bundled-subagents pattern"), not cite internal D-numbers.

## Rules

- Push and `gh pr create` happen only with explicit user approval at GATE 7.
- `propose_decision` happens only with explicit user approval. Subagents draft or surface decisions; they never file an unapproved one. After the user approves in chat, the originating agent files — the planner for plan-time decisions, `@nauro-tech-lead` for doctrine moves it surfaces in Mode C. The executor never files; it surfaces emergent choices in its handoff (step 3) for the parent to gate. The kernel commits immediately on Tier 1 clean.
- If a `propose_decision` is pending but the Nauro MCP server is disconnected, that is a hard pause — do not push the PR and file the decision after reconnecting. The code and its decision land together; a push-now-file-later split leaves doctrine unrecorded if the session ends first. Surface the disconnect and wait.
- If anything fails or surprises mid-chain (a tool errors, tests fail unexpectedly, a verdict is incoherent), stop and surface to the user rather than recovering silently.
- The chain is doctrine-aware end-to-end: every architectural choice flows through `check_decision` (at planning) and `propose_decision` (when the choice lands, after user approval). Skipping either silently is a chain failure, not a shortcut.
