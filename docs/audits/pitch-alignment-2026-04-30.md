# Pitch alignment audit — "Reins on AI-built projects"

**Date:** 2026-04-30
**Auditor:** Claude (audit-reins-pitch branch)
**Scope:** `nauro-ai/nauro` monorepo (CLI + nauro-core + cross-surface copy). The remote MCP server in the private `mcp-server` repo is referenced where it consumes shared registries but not audited directly.
**Constraint:** Reconnaissance only. No code changes.

The new pitch frames Nauro as the *reins* on AI-built projects — a steering layer that lets a developer set project direction once and have every agent inherit it. Headline: "Set the vision once. Every agent inherits it." `check_decision` is the centerpiece — the "tug on the reins" when an agent is about to violate a prior decision.

This audit compares each user-facing surface and several architectural claims against the new framing, and identifies conflicts with prior decisions (notably **D095**, which locked "theory" as the primary framing word and banned "memory" from self-description).

---

## 1. Aligned — surfaces where code already matches the new pitch

### 1.1 README.md hero already does the conflict-catching work

**File:** `README.md:5`
> "When an agent proposes an approach that conflicts with a past decision, Nauro catches it before the drift happens."

This is the reins thesis in one sentence. The verb "catches" and the noun "drift" are both reins-aligned. Keep this clause regardless of any tagline change.

### 1.2 README demo prompt is reins-shaped, not lookup-shaped

**File:** `README.md:30-32`
> Open Claude Code and ask: *"Check if we should add a WebSocket endpoint for live task updates"*
> The demo creates a sample project with 7 decisions, project state, and open questions. `check_decision` surfaces a conflict: the team already chose SSE over WebSocket because persistent connections weren't released during ECS rolling deploys.

The demo asks the agent to evaluate a proposal, not to recall a fact. This is exactly the reins moment the pitch wants on the homepage. It contrasts cleanly with the dated prompt in `packages/nauro/README.md:32` ("What did we decide about the database?"), which is passive recall — see §2.6.

### 1.3 README competitive contrast already names the reins behavior

**File:** `README.md:60`
> "Memory tools record what agents saw and said. Nauro captures what you decided and rejected, then checks every session against those decisions before they drift."

Reads as steering language ("checks every session... before they drift"). This sentence is locked by D095 ("competitive contrast" line) and is fully compatible with the new pitch — keep verbatim.

### 1.4 Pipeline naming is explicitly highlighted

**File:** `README.md:69`
> "The `propose_decision` → `confirm_decision` → `check_decision` pipeline catches conflicts across any connected surface."

The pitch's centerpiece pipeline gets explicit prose treatment. Note: ordering is correct for the *write* flow but a reader may not realize that a typical agent session begins with `check_decision`, not ends with it (see §3.2).

### 1.5 MCP `instructions` field puts `check_decision` first by topic order

**File:** `packages/nauro-core/src/nauro_core/constants.py:73-80`
The static instruction block's first behavioural section is "## When to check decisions", and it explicitly says: *"Before adopting any technical approach... call `check_decision`..."* This is the right ordering for the pitch — `check_decision` is the first action an agent is told to take. Architecturally, this gives the centerpiece the structural prominence the pitch claims.

(The framing of *what Nauro is* in line 70 is wrong — see §2.1 — but the `check_decision`-first order is correct.)

### 1.6 Cross-surface neutrality is structurally honored

**Files:** `packages/nauro-core/src/nauro_core/mcp_tools.py` (whole file), `packages/nauro/src/nauro/cli/commands/setup.py:147-156`

D050 (cross-surface positioning) and D101 (move behavioral guidance from CLAUDE.md to MCP `instructions`) demand that no MCP client gets preferential treatment. The shared tool registry in `nauro_core.mcp_tools` is consumed identically by stdio and HTTP servers; `setup.py` actively *removes* the legacy CLAUDE.md block on every run (lines 147-156). There is no Claude-Code-specific behaviour buried in the tool surface today. The "every agent inherits it" claim in the new headline is structurally honest.

The one residual Claude-Code-specific surface is the `nauro setup claude-code` command itself — but that is a config-installer for one client, not a behavioral asymmetry.

### 1.7 `check_decision` description includes the user-prompt triggers from the pitch

**File:** `packages/nauro-core/src/nauro_core/mcp_tools.py:251-256`
> *"Use this to consult the project's decision history before committing to an approach — especially when the user asks 'should we...', 'what if we...', 'can we...', or 'check if...'."*

The "should we / what if we / can we / check if" triggers are the exact reins moments. The description is concrete and behaviorally specific — well-placed for the centerpiece.

### 1.8 `project.md` scaffold captures direction, not just history

**File:** `packages/nauro/src/nauro/templates/scaffolds.py:28-46`
The scaffolded `project.md` prompts for: one-liner, goals (with "what success looks like in concrete terms"), non-goals, users, constraints. This is direction-shaped, not memory-shaped — precisely the "vision" the pitch wants set once. The `get_context` payload returns this verbatim, so vision *does* propagate to every agent.

---

## 2. Misaligned — surfaces where code reflects older framings

These findings are split into two categories: **(a) surface-level copy** that can change without architectural impact, and **(b) structural** misalignments where behaviour does not match a pitch claim.

### 2.1 [Copy / High impact / D095 conflict] MCP `instructions` opens with "decision memory"

**File:** `packages/nauro-core/src/nauro_core/constants.py:69-71`
```python
MCP_INSTRUCTIONS_STATIC = """\
Nauro is the project's decision memory. Use it to check past decisions \
before committing to an approach, and to record new decisions as you make them.
```

This is the **first sentence every connected MCP client reads about Nauro** — delivered via the `initialize` handshake to Claude Code, Claude.ai, Cursor, Perplexity, ChatGPT and any future MCP client. Three problems:

1. **Direct conflict with D095** — D095 explicitly bans "memory" from self-description ("'Memory' is banned from self-description — every competitor uses it (MemPalace, Letta, mem0, platform features), causing instant mis-categorization").
2. **Direct conflict with the new pitch** — "AI memory" is the framing the pitch is moving *away from* ("too passive, wrong category").
3. **Behavioral cost** — agents read this as a category cue. "Decision memory" tells them to *recall*; the reins framing tells them to *check before acting*. The category mismatch likely degrades agent behaviour even before any copy change.

This is the highest-impact single line in the audit. Everything else is downstream.

**Suggested replacement (one line):**
> "Nauro is the steering layer for this project. Use it to check past decisions before adopting an approach, and to record new decisions as you make them."

(Note: "steering layer" hits the reins framing without naming Naur or theory; the rest of the block is already fine.)

### 2.2 [Copy / D095 conflict] README hero one-liner is the locked D095 wording

**File:** `README.md:3`
> "Give every AI agent your project's theory: the decisions, rationale, and rejected paths."

This is the exact one-liner D095 locked in (D095: "*One-liner (≤15 words): 'Give every AI agent your project's theory — the decisions, rationale, and rejected paths.'*"). The new pitch wants "Set the vision once. Every agent inherits it." instead.

This is a **copy change at the surface level**, but it touches a logged decision. Treating it as a copy edit silently overrides D095. Treat it as a decision-level supersession instead — see §4 and the proposed-decision draft in §5.

### 2.3 [Copy / Outdated framing] README footer in AGENTS.md still says "agentic development"

**File:** `packages/nauro/src/nauro/templates/agents_md.py:82`
```python
"*Generated by [Nauro](https://nauro.dev) — project context for agentic development.*"
```

D056 (and downstream D050) explicitly moved positioning *away from* "agentic development" toward cross-surface neutrality. This footer was missed during D056. Independent of the new pitch, this string is already misaligned with two prior decisions.

**Suggested replacement:**
> *"Generated by [Nauro](https://nauro.dev) — project context for every AI tool you use."*
(Or whatever final tagline lands — but anything other than "agentic development".)

### 2.4 [Copy] CLI top-level help is generic

**File:** `packages/nauro/src/nauro/cli/main.py:22, 38`
```python
help="Local CLI for managing versioned project context for AI coding agents."
```

"Versioned project context for AI coding agents" is generic and pre-D050. The new pitch wants direction/steering language; D050 wants cross-surface (not "coding agents" exclusively).

**Suggested replacement (one line):**
> "Set your project's direction once and steer every AI agent that touches it."

### 2.5 [Copy] PyPI description for `nauro` echoes the same generic framing

**File:** `packages/nauro/pyproject.toml:8`
```toml
description = "Local CLI + MCP server that maintains versioned project context for AI coding agents."
```

Same fix as §2.4. PyPI descriptions are seen by every install path (`pip search`, IDE package suggestions, hosted indices). They should match the new pitch.

`nauro-core/pyproject.toml:8` ("Shared pure-Python logic for Nauro: parsing, validation, context assembly, constants.") is a utility-library description and is fine — `nauro-core` is not where the pitch happens.

### 2.6 [Copy / Outdated content] `packages/nauro/README.md` is significantly stale

**File:** `packages/nauro/README.md` (entire file vs. top-level `README.md`)

The package-local README diverges from the org-level README in several positioning-relevant ways:

- **Line 3:** *"Persistent project context for AI coding agents."* — pre-D050, no reins/direction framing.
- **Line 5:** *"Think `git log` for *why* your project is the way it is."* — passive ("the way it is"), not directional.
- **Line 32:** Demo prompt is *"What did we decide about the database?"* — passive recall, not the reins-shaped prompt the top README uses.
- **Line 68:** Comparison table includes a *"Claude Memory"* row labeled "Proprietary" — the top README dropped this row, presumably to avoid the "memory" anchor word.
- **Line 115:** *"Free tier: unlimited local usage + 100 remote MCP calls/month."* — outdated against **D088** (5,000/month).
- **Lines 40-45:** Cross-surface flow doesn't mention `nauro link --cloud` — also outdated.

This file is published with the `nauro` PyPI package as the long_description and shows up on pypi.org/project/nauro. It is a high-traffic surface that has drifted from the org README by months. Recommendation: replace this file with a thin pointer to `../../README.md`, or rewrite to match.

### 2.7 [Copy] Onboarding empty-state guidance frames Nauro as a recorder, not a steerer

**File:** `packages/nauro/src/nauro/onboarding.py:8-17`
```python
WELCOME_NO_PROJECT = (
    "Welcome to Nauro! No project store found.\n"
    ...
    "Your decisions will then be available here and across all connected AI tools."
)
```

The string ends with an availability promise, not a steering promise. New users reading this learn that Nauro *stores and serves*, not that it *checks and steers*. Since this is the first multi-line message a brand-new user sees from inside an agent session, it should preview the reins behavior.

**Suggested replacement of the closing line:**
> "Once you log decisions, every agent in this project will check against them before drifting."

Same applies to `NO_DECISIONS_TO_CHECK` (lines 35-41) and `NO_CONTEXT_YET` (lines 43-50) — both frame the loop as "record so you can read", not "record so future agents stay on course".

### 2.8 [Copy] Tool descriptions are functional, not directional — for several tools

**File:** `packages/nauro-core/src/nauro_core/mcp_tools.py`

Specific descriptions where the language is descriptive rather than direction-aligned:

- **`get_context` (lines 68-80):** "Return project context at the requested detail level... project's goals, constraints, and recent history." → mostly aligned (covers goals/constraints) but reads like a context-loader. Could lean into "the project's direction" or "where the project is going" to surface vision explicitly.
- **`propose_decision` (lines 281-300):** "Propose a new architectural decision for validation and recording." → "validation and recording" is process-shaped. The reins frame would emphasize *committing direction* before agents drift.
- **`confirm_decision` (lines 366-372):** purely procedural ("Only needed when propose_decision returns status=pending_confirmation"). OK as-is — confirmation is a process step, not a moment of direction.
- **`update_state` (lines 422-430):** see §2.9 — substantively misaligned.
- **`flag_question`, `search_decisions`, `list_decisions`, `get_decision`, `get_raw_file`, `diff_since_last_session`:** all neutral procedural descriptions — fine.

`check_decision` description (lines 245-256) is the one that *is* directional today — it should be the model the others move toward, not the exception.

### 2.9 [Structural] `update_state` tracks progress, not direction

**Files:**
- Description: `packages/nauro-core/src/nauro_core/mcp_tools.py:419-430` — *"Update the project's current state with a progress delta..."*
- Scaffold: `packages/nauro/src/nauro/templates/scaffolds.py:48-55` — `state.md` "Current" placeholder is *"Building user auth flow, blocked on Stripe API approval"*.
- Implementation: `packages/nauro/src/nauro/mcp/tools.py:400-431` — only stores delta strings, no direction field.

The pitch asks: *"Does `update_state` track direction, or only progress?"* Today, only progress. The state object is a feed of "what changed", with no "where are we headed" hook. This is a structural, not copy, misalignment — `update_state` cannot surface direction even if its description were rewritten, because there is no direction-shaped slot in the state.md template or in the tool's signature.

That said: the pitch may not need this to change. `project.md` (via `get_context`) already carries direction (goals, non-goals, constraints). State and direction are arguably separable: `update_state` covers "where we are right now", `project.md` covers "where we are going". If the pitch is fine with that split, this is a copy fix in the description (not "progress delta" — say what state actually is). If the pitch genuinely wants direction tracking inside state, that is a data-model change and belongs in a separate decision. Recommendation: clarify the split in copy first; defer any structural change.

### 2.10 [Structural / Mild] `check_decision` is 7th in the registry, last in the README's read list

**Files:** `packages/nauro-core/src/nauro_core/mcp_tools.py:466-479`, `README.md:114-121`

`ALL_TOOLS` registry order:
```
GET_CONTEXT, GET_RAW_FILE, LIST_DECISIONS, GET_DECISION,
DIFF_SINCE_LAST_SESSION, SEARCH_DECISIONS, CHECK_DECISION,  ← 7th
PROPOSE_DECISION, CONFIRM_DECISION, FLAG_QUESTION, UPDATE_STATE, LIST_PROJECTS
```

The README's "Read" list also presents `check_decision` last among reads.

This is partial-alignment, not a hard misalignment. MCP clients render tools in `tools/list` order (and most of them sort or otherwise ignore registry order), so the visual prominence cost is mild. **But the instructions field already gives `check_decision` correct prominence by topic order (§1.5)**, so the registry is the lower-leverage fix.

If the pitch wants `check_decision` to be unambiguously the centerpiece in every artifact, consider:
- Moving `CHECK_DECISION` to be the first non-context entry in `ALL_TOOLS` (after `GET_CONTEXT` and `LIST_PROJECTS`).
- Reorganizing the README's MCP tool list to lead with `check_decision` followed by `propose_decision`/`confirm_decision`, then group reads.

This is a polish move, not a correctness fix.

### 2.11 [Out of scope] Show HN landing page (React app)

The audit prompt asked about *"the Show HN landing page (React app)"*. **No React/landing/website app exists in this repo** (`Glob "**/*.tsx"`, `**/*.jsx"`, `**/landing/**`, `**/blog/**`, `**/onboarding/**` all empty). Either the landing page lives in a separate repo (likely the same place as `mcp-server/`), or it has not been built yet. Audit cannot evaluate it from here. **Flag for the human:** decide whether the landing page is a separate repo to be audited next, or scope-out for v1.

---

## 3. Architectural alignment — does the code support the pitch's claims?

This section answers Step 3 of the audit prompt directly.

### 3.1 Is `check_decision` actually the centerpiece?

**Partly. Yes by instruction order; no by registry/list order.**

- ✓ The MCP `instructions` field tells agents to call `check_decision` *first*, before any architectural change (§1.5).
- ✓ Its description (§1.7) names the user-prompt triggers ("should we / what if we / can we / check if").
- ✗ It is 7th in the tool registry and last in the README's read list (§2.10).
- ✗ The README's prose features the pipeline as `propose → confirm → check` (line 69), not `check → propose → confirm`. The order in prose mirrors the *write* flow, not the typical session flow.

**Verdict: the pitch's "centerpiece" claim is honest at the behavioral guidance layer, modestly under-supported at the listing/discovery layer.** The instructions field is the right load-bearing surface; the registry order and README list order are polish.

### 3.2 Does the propose → confirm → check pipeline have the structural prominence the pitch implies?

**Yes structurally; the README's framing slightly obscures the typical-flow order.**

The three tools are explicitly grouped and named as a pipeline in the README (line 69). They are validated end-to-end in `packages/nauro/src/nauro/validation/pipeline.py`. The decision write-path is genuinely the "tier-1 → tier-2 → tier-3" pipeline the description promises.

Caveat: in a real session, the flow is usually `check_decision (advisory) → user decides → propose_decision (with skip_validation=true if check was clean) → confirm_decision`. The README pipeline name is in write order, which is correct for the write flow but obscures the typical entry point. Suggested rewording in the README without changing semantics:
> "The `check_decision` → `propose_decision` → `confirm_decision` pipeline checks every approach against past decisions before recording new ones, across any connected surface."

### 3.3 Does `get_context` surface project *direction*?

**Yes — through `project.md`.**

- `project.md` scaffold (§1.8) prompts for goals, non-goals, users, constraints — all direction-shaped.
- `build_l0_payload` (used by `get_context` at L0) includes the project summary in every payload.
- Therefore: every agent that calls `get_context` at session start receives the project's stated direction (goals, non-goals, constraints) as the first thing in the response.

**Verdict: aligned.** Direction is in `project.md`; `get_context` returns `project.md`; agents see it.

The one weak link: scaffolded `project.md` is a template with bracketed prompts. Users who skip filling it in get a payload full of `[Primary goal — what success looks like...]` placeholders, which doesn't steer anything. Worth flagging for onboarding UX, but that's outside the pitch's claims.

### 3.4 Does `update_state` track direction?

**No — it tracks progress only.** See §2.9. Whether this is a problem depends on whether the pitch needs state to carry direction in addition to `project.md`. Recommend clarifying in copy that state is "where we are" and `project.md` is "where we are going", not pretending state is direction-tracking.

### 3.5 Cross-surface neutrality (D050) — does the code actually treat all MCP clients as first-class?

**Yes.** See §1.6. The shared registry in `nauro_core.mcp_tools` is the structural enforcement; D101 actively moved guidance out of CLAUDE.md so Cursor/Perplexity/ChatGPT users are not second-class. The only Claude-Code-specific code is `nauro setup claude-code`, which is a config-installer for one specific client (parity with what the user would do by hand for any other MCP client). The moat argument from D050 is honest in the current code.

---

## 4. Conflicts with prior decisions

The new pitch creates **one explicit conflict** with a logged decision and **one conflict with previously-locked copy** that needs decision-level handling rather than a copy edit.

### 4.1 D095 — "theory" as primary framing word, "memory" banned

**Decision:** D095 (2026-04-11, active, confidence: high) — locked: one-liner, tagline ("Every agent starts with the why."), Show HN title, competitive contrast, and copy rules including the Naur reference treatment.

**Conflict points with the new pitch:**
1. New pitch *moves away from* "theory" as primary user-facing framing. D095 made "theory" load-bearing ("**competitively essential**" because Lodestar claimed "decision layer").
2. New pitch retains the "memory" rejection — same as D095, no conflict on that side.
3. New pitch keeps the active `check_decision` enforcement claim — also same as D095, no conflict.

D095's reasoning for "theory" was explicitly **competitive**: Lodestar (April 2026) had claimed "decision layer", forcing Nauro to a different unclaimed word. If the competitive landscape has changed since, or if the new "reins/steering" framing has been validated as superior with the target audience, that warrants a fresh decision. If the change is purely creative, it may walk back into the same competitive trap that led to D095.

**Resolution path (proposed, not implemented):** treat as a partial supersession — see §5.1. D095 carries five distinct copy decisions (one-liner, tagline, Show HN title, competitive contrast, Naur reference rules); only the *primary framing word* needs to flip from "theory" to "vision/direction/steering". The Naur reference treatment, the "memory ban", and the competitive contrast can survive.

### 4.2 D056 — "Memory" not the only banned framing; "agentic development" was the *previous* primary framing being retired

**Decision:** D056 (2026-03-28, active, confidence: high) — README rewrite mandated cross-surface positioning, retiring "the project context service for agentic development".

**Conflict point:** None directly with the new pitch. But the audit surfaced (§2.3) that `packages/nauro/src/nauro/templates/agents_md.py:82` still emits *"project context for agentic development"* in every generated AGENTS.md footer. This is residue D056 should have already cleaned up. It is a copy fix, not a new decision, but worth listing under "conflicts" since it represents a decision (D056) that did not fully land in code.

### 4.3 D050 — cross-surface positioning

**Decision:** D050 (2026-03-24, active, confidence: high) — Nauro serves all AI surfaces, not just coding.

**Conflict point:** None. The new pitch's "every agent inherits it" preserves cross-surface; nothing in the new framing names a single vendor or single client. **Aligned.**

### 4.4 D092 — moat risk reassessment

**Decision:** D092 (2026-04-03, active, confidence: medium) — moat pillars include "decisional depth over observational memory" as Nauro's "most defensible wedge", with review date Oct 1, 2026.

**Conflict point:** None directly. The new pitch's "tug on the reins" centers the `check_decision` enforcement behavior, which *is* decisional depth in action. If anything, the reins framing makes the wedge more visible. **Aligned, possibly amplified.**

---

## 5. Draft `propose_decision` payloads (pending — for human review)

The audit surfaces one decision-level shift that should be logged, plus one optional cleanup-decision about retiring AGENTS.md footer phrasing.

**These are drafts for the human to review and submit via `propose_decision` — not to be auto-confirmed.**

### 5.1 Draft: Partial supersession of D095 — "vision/direction/steering" replaces "theory" as the primary user-facing framing word

**Title:**
> Primary user-facing framing word shifts from "theory" to vision/direction/steering ("reins" pitch); D095 partially superseded

**Rationale:**
> The new pitch frames Nauro as the reins on AI-built projects — a steering layer that lets a developer set project direction once and have every agent inherit it. Headline: "Set the vision once. Every agent inherits it." Mental model: rider on a strong horse — reins for direction, not a brake.
>
> D095 (2026-04-11) locked "theory" as the primary framing word, citing Lodestar's "decision layer" occupancy as the *competitive* reason "theory" was strategically necessary. The new pitch supersedes that choice for three reasons:
>
> 1. **Action over abstraction.** "Theory" describes what is captured (the why-layer); "reins/steering/direction" describes what Nauro does to agents (corrects course before drift). The pitch's thesis is that the active behavior — `check_decision` — is the wedge, not the captured artifact. Framing should match the active claim.
> 2. **Lower abstraction cost.** D095 noted that "theory" required always-on expansion ("your project's theory: the decisions, rationale, and rejected paths") to avoid abstraction risk. Reins/steering/direction is concretely physical and does not need expansion to land.
> 3. **Centerpiece visibility.** Reins framing makes `check_decision` the headline behavior. Theory framing put `check_decision` in the supporting cast.
>
> Scope of supersession from D095:
> - **Superseded:** primary framing word ("theory" → "vision/direction/steering"), one-liner ("Give every AI agent your project's theory..." → "Set the vision once. Every agent inherits it."), tagline, Show HN title (deferred — Show HN copy needs its own decision once a final landing page exists).
> - **Retained from D095:** "Memory" ban from self-description (still correct under the new framing — competitor mis-categorization risk unchanged). Naur paragraph-3 origin-story treatment (still appropriate as background, never as primary framing). Competitive contrast wording at `README.md:60` (still aligned with reins).
> - **Retained from D050:** cross-surface positioning thesis ("every agent" preserves vendor neutrality).
>
> Implementation footprint (rough):
> - Required: `packages/nauro-core/src/nauro_core/constants.py:69-71` (MCP `instructions` opening); `README.md:3` (hero one-liner); `packages/nauro/src/nauro/cli/main.py:22, 38` (CLI top-level help); `packages/nauro/pyproject.toml:8` (PyPI description); `packages/nauro/src/nauro/templates/agents_md.py:82` (footer).
> - Recommended: `packages/nauro/README.md` (full rewrite or replace with redirect to org README); `packages/nauro-core/src/nauro_core/mcp_tools.py` tool descriptions for `get_context`, `propose_decision`, `update_state`; `packages/nauro/src/nauro/onboarding.py` empty-state strings.

**Rejected alternatives:**
- *Full supersession of D095, including retiring the Naur reference entirely* — the Naur reference is origin-story-only in the new pitch, not load-bearing for primary framing. There is no reason to discard it. The new framing operates above the academic reference; the reference adds depth without competing for the headline.
- *Treat as a copy edit, not a decision* — D095 was a deliberate competitive move with explicit rationale (Lodestar pressure). Walking back the primary framing word silently risks repeating the original problem (mis-categorization). Decision-level handling forces an explicit recheck of the competitive landscape.
- *Keep "theory" as primary framing and add "reins" as a secondary metaphor* — the pitch claims reins is the headline mental model. Layering it under "theory" defeats the structural shift the pitch is making.

**Suggested fields for `propose_decision`:**
- `decision_type`: architecture
- `confidence`: medium (the pitch is recent; needs validation against the same target audience D095 used)
- `reversibility`: easy (copy changes; any code restructuring suggested in §2 is small)
- `files_affected`: list above

### 5.2 Draft (lower priority): Retire residual "agentic development" phrasing not caught by D056

**Title:**
> AGENTS.md footer drops "project context for agentic development" — D056 cleanup

**Rationale:**
> D056 (2026-03-28) mandated retiring "agentic development" framing from user-facing surfaces in favor of cross-surface positioning. The footer in `packages/nauro/src/nauro/templates/agents_md.py:82` still emits *"Generated by Nauro — project context for agentic development."* on every regenerated AGENTS.md. This is D056 residue that was missed; cleanup belongs to that decision's mandate, not a new strategic shift.

**Decision:** This may not need a decision-level proposal — it is a code-edit-level cleanup that D056 already authorized. If the human prefers to leave the audit surface clean, batch this with §5.1's implementation rather than logging a separate decision.

**Rejected alternatives:**
- *Make it a separate decision* — D056 already authorizes the cleanup; logging a second decision adds noise.

---

## Summary punch list (for the human)

| # | Where | Severity | Type | Action |
|---|---|---|---|---|
| 2.1 | `packages/nauro-core/src/nauro_core/constants.py:70` | **Highest** | Copy + D095 conflict | Replace "decision memory" — every MCP client reads this |
| 2.2 | `README.md:3` | High | D095 conflict | Hero one-liner — needs decision-level supersession (§5.1) |
| 2.3 | `packages/nauro/src/nauro/templates/agents_md.py:82` | Medium | D056 residue | "agentic development" footer — already authorized to clean up |
| 2.4 | `packages/nauro/src/nauro/cli/main.py:22, 38` | Medium | Copy | CLI top-level help generic |
| 2.5 | `packages/nauro/pyproject.toml:8` | Medium | Copy | PyPI description generic |
| 2.6 | `packages/nauro/README.md` (whole file) | High | Copy + outdated | Stale package README — drifted from org README; also wrong on D088 quotas |
| 2.7 | `packages/nauro/src/nauro/onboarding.py:8-50` | Low | Copy | Empty-state strings frame as recorder, not steerer |
| 2.8 | `packages/nauro-core/src/nauro_core/mcp_tools.py` (multiple) | Low | Copy | Descriptions are functional, not directional — for `get_context`, `propose_decision`, `update_state` |
| 2.9 | `update_state` (description + scaffold + impl) | Medium | **Structural** | Tracks progress, not direction — verify whether the pitch wants direction in state, then either reword or scope a data-model change |
| 2.10 | `packages/nauro-core/src/nauro_core/mcp_tools.py:466-479`, `README.md:114-121` | Low | **Structural** | `check_decision` is 7th in registry, last in README — polish to make it visually first |
| 2.11 | landing page (React app) | Out of scope | N/A | Does not exist in this repo — confirm whether to audit a separate repo |
| 5.1 | n/a | High | Decision | Submit `propose_decision` for partial D095 supersession |

The single highest-leverage change is §2.1: every connected MCP client opens with "Nauro is the project's decision memory." Fix that and the pitch's behavioral framing reaches every agent immediately, with no other code change required.
