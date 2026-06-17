---
name: nauro-loop
description: Run a gated iteration of work origination on top of /nauro-ship-task. Invoke under the dynamic /loop command (/loop /nauro-loop). Mines the project's existing Nauro store state read-only (get_context, open-questions RESUME/BRIEF pointers, diff_since_last_session, list_decisions) and originates 1-3 ranked candidate tasks, then surfaces them via AskUserQuestion for the human to pick — a mandatory ratify-gate with no auto-pick path. On the human's pick it dispatches /nauro-ship-task <chosen task> byte-for-byte with all six inner gates intact, then loops back. Originates the candidate set only; the human selects the task, approves the plan, clears every tech-lead pause, and confirms every push. The loop itself never files a decision, never pushes, and never runs gh; it holds no store-write authority. Stops on an empty mine and at a hard per-session ceiling. Installed by `nauro adopt --with-skills`.
---

# Nauro loop skill

Run a gated iteration of work origination on top of `/nauro-ship-task`. This skill is a thin outer loop invoked under the dynamic `/loop` command (`/loop /nauro-loop`). It mines the project's existing Nauro store state for candidate work, ranks a small set, and surfaces it to the human to pick; on the human's pick it dispatches `/nauro-ship-task <chosen task>` byte-for-byte with all six inner gates intact, then loops back. It originates the candidate set; the human selects, approves, and confirms everything downstream.

The loop adds one net-new agent authority — task origination — and nothing else. Today the human authors the task description handed to `/nauro-ship-task`; the loop now proposes and ranks the candidate "what to build next" set. It enumerates options; it never selects. Everything past enumeration stays with the human: the SELECT ratify-gate, the chain's plan-approval gate, every AMBER/RED tech-lead pause, and every push confirmation.

## What the loop cannot do

These are structural, not stylistic. The loop holds none of these capabilities and must not simulate them.

- The loop never files a decision. It holds no write authority into the store, cannot record doctrine, and carries no path to commit one. It runs in the main-agent context with no tool-lock, so an autonomous filing would install binding doctrine with no human gate; the only filing that ever happens is inside the dispatched chain, after a human clears a chain gate, by the agent the chain assigns.
- The loop never pushes and never runs `gh`. Push and PR creation live only inside the chain's push-confirmation gate, behind an explicit human "yes".
- `/loop` is NOT a "keep moving" override of any inner gate. A standing "keep going" or auto-mode directive does not clear the SELECT gate, the plan gate, a tech-lead pause, or the push gate. The loop exists to repeat the gated chain, not to bypass it.
- Under the loop, the chain's low-stakes auto-proceed path at the plan gate is CLOSED. Inside a bare `/nauro-ship-task` run a plan with no doctrine writes and no high-stakes triggers may auto-proceed to the executor; under `/loop` that path is closed and every plan blocks for explicit human approval at the plan gate. Tightening origination this way is doctrine-positive, not a regression.
- The loop fails closed on a gate-callback timeout. If a human gate is surfaced and the response channel times out or is unavailable, the loop halts and surfaces the held state; it never treats a timed-out gate as an approval.
- A held gate takes a lock: while any gate (SELECT or an inner chain gate) is awaiting a human, the loop starts no new ORIENT, composes no new candidates, and dispatches no chain. One gate is open at a time.
- The loop has a hard per-session ceiling on both completed chains and idle re-orient cycles. When either ceiling is reached, the loop stops and reports; it does not silently continue.

## ORIENT — mine the store, read-only

ORIENT writes nothing. It reuses the Resume R1/R2 mining logic to read the project's current state and assemble candidate work:

- `get_context(level="L0")` for the concise project summary — current state, the top open questions, and last-10 active-decision summaries. That is enough to rank candidates against current direction; ORIENT does not need full decision bodies to compose the set, so it takes the cheaper L0 projection rather than the larger working set.
- `get_raw_file(path="open-questions.md")`, scanned for the `RESUME:` and `BRIEF:` markers — a `RESUME:` marker names in-flight work to continue; a `BRIEF:` marker names context another agent left that may seed a task. This scan stays even though ORIENT already read L0: L0 deliberately excludes the discovery pointers from its open-questions projection, so the markers never appear in the L0 payload and a separate targeted scan of the file is the only way to reach them. Scanning a large file for two literal markers is cheap; reading the whole file into context is what overflowed, so scan for the markers rather than ingesting the file whole.
- `diff_since_last_session` to see what changed recently, so the candidate set reflects real movement and not a stale read.
- `list_decisions` to ground candidates against active doctrine and recent direction.

From that, ORIENT composes 1-3 ranked candidate tasks. Each candidate carries a one-line rationale, the source signal it came from (the `L0` working set, a specific pointer, a recent diff, a decision), and its provenance so the human can trace where it originated.

Re-verify every `RESUME:` anchor before ranking it: check the branch heads, open PR numbers, and any expected-state anchors the pointer names against `origin/main`. A `RESUME:` candidate whose anchors no longer match is demoted to "stale, surface" — it is not ranked as live work; it is reported to the human as a pointer that needs attention. ORIENT never fabricates a candidate: if the mine is empty, it composes nothing and the loop stops (see RE-ORIENT).

## SELECT — GATE — the human picks (mandatory, no auto-pick ever)

SELECT surfaces the ranked candidates and waits for the human to choose. This gate is mandatory and has no auto-pick path — not even when exactly one candidate ranks. Removing the human from selection would begin removing the human from origination, which the loop must never do.

Surface the candidates through `AskUserQuestion`, presenting each candidate with its one-line rationale, its source signal, and its provenance. If ORIENT ran `check_decision` against a candidate, show its output as a raw related-decision list only — never a verdict, score, or recommendation. `check_decision` returns related decisions; it does not judge, and the SELECT surface must not present it as if it did. The human reads the candidates and the related decisions and decides.

The human may pick one candidate, or reject all of them. On rejection the loop surfaces that the mine produced nothing the human wanted and stops; it does not silently re-rank the same set. The human's chosen candidate becomes the verbatim input to `/nauro-ship-task` — the loop passes the task description through as the human ratified it, not a paraphrase.

## CHAIN — dispatch /nauro-ship-task byte-for-byte

On the human's pick, dispatch `/nauro-ship-task <chosen task>` exactly as written, with all six existing gates intact: the RED-supersede pause before the executor, the plan-approval gate, AMBER surfacing, the RED tech-lead pause, the push-confirmation gate, and the doctrine-disconnect hard-pause. Do not reproduce the chain inline in the loop — the gates depend on the chain's structure and the bundled subagents' restricted tool access, and an inline imitation runs without them. The loop's job is to hand the ratified task to the chain and let the chain run; the loop adds no step inside the chain and removes none.

Every filing, every PR, and every push during the chain happens inside the chain after a human clears the relevant chain gate. The loop neither files nor pushes on its own.

## INTEGRATE — record nothing to the store

When the chain returns, INTEGRATE writes nothing to the Nauro store. The chain has already landed whatever it landed (a local commit, an opened PR on the human's push) and filed whatever the human approved inside it. The loop holds no write path, so there is nothing for it to record; it carries the chain's outcome forward only as in-session state for the next ORIENT.

## RE-ORIENT — loop back, or stop

RE-ORIENT runs ORIENT again to mine the now-changed store for the next candidate set. The loop stops, without fabricating work, when either condition holds:

- The mine is empty — ORIENT composes no candidate. The loop reports that there is no further work it can originate and stops. It does not invent a task to keep the loop running.
- The per-session ceiling is reached — the count of completed chains or idle re-orient cycles hits the hard cap. The loop reports the ceiling and stops.

## Gate H — assistance / stuck

If the dispatched chain self-halts or fails loud — a capped fix loop exhausts, a tech-lead RED with no human override, a tool error, a doctrine-disconnect hard-pause, an incoherent verdict — the loop does not retry blindly and does not move to the next candidate. It surfaces the halted state and the chain's reported reason to the human and waits. Gate H is the loop's stuck-handler: a chain that stops loud is a signal for the human, not a cycle to absorb silently.

## Substrate and scope

The loop runs under the dynamic `/loop` command (`/loop /nauro-loop`), which repeats the skill in the parent session and can pause for the SELECT gate's `AskUserQuestion`. Unattended substrates — cron, scheduled wakeups, or cloud routines — are out of scope: a run on those cannot pause for the human sign-off the SELECT and chain gates require, so the loop is not wired onto them.

## Inherited external review

The dispatched chain offers an optional external second-opinion review at its push gate. That step is off by default and offer-only: the chain offers it per push, the human opts in, and the findings are advisory — never a blocking gate. The loop inherits it for free because it dispatches the chain byte-for-byte, and the loop never auto-accepts it on the human's behalf. If no external-review skill is wired in the environment, the chain does not offer the step.

## Rules

- The loop never files a decision, never pushes, and never runs `gh`. It holds no store-write authority and cannot record doctrine; all of that lives inside the dispatched chain behind a human-cleared gate.
- SELECT is mandatory with no auto-pick path ever — not even for a single ranked candidate. The human selects every task; the loop only enumerates.
- `/loop` does not override any gate. Auto-mode and standing "keep moving" directives clear neither the SELECT gate nor any inner chain gate, and under the loop the chain's low-stakes auto-proceed at the plan gate is closed — every plan blocks.
- ORIENT is read-only and never fabricates a candidate; on an empty mine the loop stops rather than inventing work.
- A `RESUME:` anchor that no longer matches `origin/main` is demoted to "stale, surface" and reported, never ranked as live work.
- The loop fails closed on a gate-callback timeout, takes a held-gate lock so only one gate is open at a time, and stops at the hard per-session ceiling on chains and idle cycles.
- A chain that self-halts or fails loud routes to Gate H — surface and wait; never retry blindly and never skip to the next candidate.
- `check_decision` output is shown as a raw related-decision list, never as a verdict, score, or recommendation.
- On any tool error or surprise mid-loop, stop and surface to the human rather than recovering silently.
