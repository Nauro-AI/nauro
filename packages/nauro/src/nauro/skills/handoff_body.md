<!-- Source template. The dogfood files under .claude/, .cursor/, .agents/
     are the rendered surface. Surface frontmatter is added by render_skill().
     This body has no protocol-fragment tokens — it composes the existing MCP
     tools get_context, update_state, flag_question, get_raw_file, and
     diff_since_last_session by name, and makes no canonical protocol claims. -->

# Nauro handoff skill

Capture a resumable session handoff into Nauro's project store, or resume a prior one, composing only the existing Nauro MCP tools. The skill has two modes. Capture, at the end of a session, writes a handoff body to the store and flags a durable resume pointer. Resume, at the start of a session, reconstructs the in-flight state from that pointer and verifies it before continuing. The skill composes `get_context`, `update_state`, `flag_question`, `get_raw_file`, and `diff_since_last_session`, and nothing else. It never files a decision.

## When to capture vs resume

Read the invoking prompt to decide the mode. Capture mode runs at the end of a working session or when the user asks to hand the session off ("hand this off", "wrap up and hand off"). Resume mode runs at the start of a session or when the user asks to pick up prior work ("pick up where we left off", "resume"). If the prompt is ambiguous, ask the user which mode they want and wait for the answer before proceeding.

v1 targets the local-store path: the agent writes the handoff to the local store on disk, then `nauro sync` pushes it. A pure chat surface with no local store cannot write an arbitrary store file, so chat-only capture is out of scope for this version. Pass `project_id` explicitly on every MCP call when more than one project exists, matching the adopt-skill convention.

## Step 1 — Capture: pull working context

The agent calls `get_context(level="L1")` to ground the handoff in the current sprint, blockers, and recent completions. The handoff reflects the project's real state, not the agent's memory of the session.

## Step 2 — Capture: write the handoff file

The agent writes the full handoff body to `<store>/handoffs/<slug>.md` using its own filesystem write. The CLI push enumerates the whole store, so a file under `handoffs/` syncs with no code change. Choose a short kebab-case `<slug>`, for example `auth-refresh-cutover`.

The handoff body states what shipped this session, what is still in flight, any branches, stashes, or worktrees left open, the cited pointers a resumer must verify (decision numbers, file paths, PR or branch names), and the next concrete step. Handoffs accumulate append-only under `handoffs/` — never overwrite a prior handoff and never delete one.

## Step 3 — Capture: state hygiene

The agent calls `update_state(delta=...)` with a short, normal session-end delta describing genuine current state. **`update_state` REPLACES `state_current.md` and is capped at roughly 5000 characters — the delta must be a one-line pointer and summary, and must never contain the handoff body.** The full handoff lives only at `handoffs/<slug>.md`; putting it in the delta would clobber the current-state surface and exceed the cap.

## Step 4 — Capture: flag the resume pointer

The agent calls `flag_question(question="RESUME: handoffs/<slug>.md — <one-line remaining-work summary>")`. This flagged question — not `state_current.md` — is the durable resume pointer. It lives in `open-questions.md`, which is append-only, so it avoids the `update_state` REPLACE-clobber surface. The `RESUME:` marker text is literal so the Resume flow can find it.

## Step 5 — Capture: GATE — confirmation (user)

The agent surfaces, in this order:

1. The handoff file path `handoffs/<slug>.md`.
2. The literal `RESUME:` marker text just flagged.
3. The session delta written to state.

Then it asks the user to confirm, and waits for explicit approval. Auto-mode and standing "keep moving" directives do not override this gate. Skipping the surface is a chain failure. Only on explicit approval does the agent proceed to the sync step.

## Step 6 — Capture: sync

On approval, the agent tells the user to run `nauro sync` from the repo, or runs it. This pushes the store so `handoffs/<slug>.md`, `state_current.md`, and `open-questions.md` travel together. This is the only step that leaves the local store.

## Step 7 — Resume: read context

The agent calls `get_context(level="L0")` for a concise orientation, then `get_context(level="L1")` for the working set, to reconstruct the project baseline before reading any handoff. L0 caps questions at the top few, so it is not relied on to surface the resume pointer.

## Step 8 — Resume: find the latest resume pointer

The agent calls `get_raw_file(path="open-questions.md")` and reads the full list to locate the most recent `RESUME: handoffs/<slug>.md — <summary>` marker. If no `RESUME:` marker exists, the agent reports "no handoff to resume" and stops.

## Step 9 — Resume: read the handoff body

The agent calls `get_raw_file(path="handoffs/<slug>.md")` for the slug named by the marker, and reads the in-flight state it records.

## Step 10 — Resume: diff since last session

The agent calls `diff_since_last_session()` to see what changed in the store since the handoff was captured. The handoff body itself is not snapshotted, so the diff is a catch-up signal on state and decisions, not on the body.

## Step 11 — Resume: reconstruct, verify, report

The agent reconstructs the in-flight state from the handoff. It verifies every cited pointer before trusting it: it confirms that referenced decision numbers, file paths, and branches still exist and match what the handoff claims. Handoff claims are hypotheses, not ground truth. The agent surfaces any drift between the handoff and current reality, then reports the reconstructed plan together with the verification results to the user before resuming work.

## Step 12 — Resume: resolve the resume question only if truly closed

The agent resolves the `RESUME:` flagged question only when a real decision has actually closed the work it pointed at. Otherwise it leaves the question open so the next session can still find it. The skill never files that decision itself.

## Rules

- The skill never files a decision. It runs in the main-agent context with no tool-lock, so autonomous filing is a hazard; it drafts decisions for the user to file and surfaces them in chat.
- `update_state` is REPLACE with a roughly 5000-character cap. The agent uses it only for normal session-end state hygiene; it never puts the handoff body in the delta. The handoff lives only at `handoffs/<slug>.md`.
- The resume pointer is a flagged question (`RESUME: handoffs/<slug>.md — <summary>`), never folded into `state_current.md` — this avoids the `update_state` REPLACE-clobber surface.
- Handoffs accumulate append-only under `handoffs/`. No pruning, no keep-N.
- On resume, the agent verifies cited pointers against current store state before acting, and reports drift rather than silently trusting the handoff.
- The capture gate is mandatory. The agent does not run `nauro sync` before the user confirms the path, the `RESUME:` marker, and the delta.
- On any tool error or surprise mid-flow, the agent stops and surfaces to the user rather than recovering silently.
