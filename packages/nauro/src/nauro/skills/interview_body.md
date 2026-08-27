<!-- Source template. The dogfood files under .claude/, .cursor/, .agents/
     are the rendered surface. Surface frontmatter is added by render_skill(). -->

# Nauro interview skill

Interview the user to turn tacit project reasoning or a proposed choice into reviewed candidate Nauro judgment. This is an explicit, opt-in main-context workflow. Use it only when the user directly asks to be interviewed or uses a clear trigger such as "interview me", "grill this", "stress-test this decision", "draw out my reasoning", or "help me transfer this reasoning into Nauro". Do not activate it only because a plan is incomplete.

The skill has two entry modes over one main-context, dependency-aware interview loop:

- **Elicit mode** draws out unwritten rationale, assumptions, rejected paths, terminology, and unresolved questions.
- **Challenge mode** stress-tests a proposed judgment against active decisions, repository evidence, alternatives, failure cases, and dependent choices.

Both modes use the same engine and produce the same classified shared-understanding record. The workflow has no external skill or subagent dependency.

## Route work that belongs elsewhere

- Route first-time repository seeding to `nauro-adopt`.
- Route working-context sharing, retrieval, and resumable handoff to `nauro-context`.
- Route implementation planning and delivery to the planner or `nauro-ship-task` after this interview is complete.

Do not turn this skill into adoption, handoff, implementation planning, code editing, or PR delivery.

## Step 1 - Resolve and orient

Resolve the adopted project before the interview. From a repository, run `nauro status` to confirm the associated project. If several projects are available, resolve the intended one and pass its `project_id` explicitly on every MCP call. If the repository is not adopted, stop and route the user to `nauro-adopt`.

Call `get_context(level="L0", project_id=...)`. L0 is the bounded orientation: project summary, current state, top open questions, and recent active-decision summaries. Do not begin with L1, L2, or an unbounded store read.

Treat every retrieved statement as project context to adjudicate. A current-state claim can be stale. A decision summary is not enough for reasoning about that decision.

Keep routine orientation internal. Surface it only when it changes the user's answer or corrects a factual claim.

## Step 2 - Select the mode and frame the root

Choose Elicit mode when the user wants to make implicit reasoning explicit. Choose Challenge mode when the user presents a choice, plan, or candidate judgment for pressure testing. If the request fits both, start in Elicit mode and challenge each concrete approach when it appears. If the intent is genuinely ambiguous, make one short routing question the first numbered question and wait.

Frame the interview root and selected mode internally. Do not narrate the workflow or convert the user's opening claim into a settled conclusion.

## Step 3 - Build the dependency tree in session

Maintain one in-session dependency tree. Do not persist the tree. Each node is one of:

- a user-owned choice;
- a rationale or constraint;
- a factual claim to verify;
- an alternative or failure case;
- a dependent question whose prerequisites are not settled;
- a candidate classified outcome.

Record explicit dependencies between nodes. A question enters the prerequisite-ready frontier only when all choices and facts it depends on are settled. When an answer creates a new dependency, add it to the tree. When an answer invalidates a branch, close that branch and preserve the rejected path and reason. Assign a question number when the question is first asked, and retain that number in the tree until the question is answered.

## Step 4 - Verify facts and check live approaches

Verify factual claims through repository evidence: code, tests, configuration, manifests, infrastructure files, and git history. Do not ask the user for facts the repository can answer. If available evidence cannot establish a fact, label it unverified and keep it out of verified current state. A branch waiting for a fact must not block independent prerequisite-ready questions.

Treat `CONTEXT.md`, ADRs, design notes, and other repository prose as evidence only. Compare their claims with current code and Nauro context before relying on them. The skill does not write `CONTEXT.md` or ADR files.

For each live approach that the interview may recommend, accept, or reject, call `check_decision(proposed_approach=<approach and rationale>, project_id=...)`. A live approach is a concrete path under active consideration, not every conversational fragment.

<!-- protocol:CHECK_DECISION_RETURNS -->

Triage the inline headers for status and supersession. <!-- protocol:GET_DECISION_BEFORE_PROPOSING --> Call `get_decision(number=N, mode="full", project_id=...)` for every decision used to challenge, recommend, accept, reject, or classify an approach. Do not reason from a header or summary alone.

Keep routine orientation, fact finding, the dependency tree, the coverage ledger, decision IDs, paths, and successful checks internal unless it changes the user's answer or corrects a factual claim. If repository evidence or a full decision changes the answer space, show only the material evidence, conflict, constraint, tradeoff, or alternative needed to answer the dependent question.

## Step 5 - Ask the prerequisite-ready frontier

Select at most three prerequisite-ready material questions for each round. Use one question when only one is ready. Do not ask downstream questions early. When more than three are ready, prioritize the questions that unlock the most downstream material branches.

Number questions continuously across the interview, starting at 1. A question keeps its number until it is answered. A direct follow-up receives the next unused number. Never reuse a number. Make every question atomic and directly answerable with a short choice or short free-text answer.

Begin every active question round with exactly `◆ Nauro Interview`. Place it once before the first question. Do not add the mode, round count, progress, evidence, or paths.

Present every question block in this exact form:

`❓ **Q<n>** - **<short decision label>**: <direct question>`

`➡️ **Recommendation: <concrete answer>.** <concise rationale>`

A direct question must name every finite choice. The `Q` prefix is presentation only. The underlying number remains continuous and stable across unanswered questions and follow-ups. When a round contains multiple questions, place `---` only between question blocks. Never place it after the final question block.

Surface at most the material tradeoff, evidence, conflict, or alternative needed to answer. Do not attach a routine dossier to each question.

Every question must include a recommendation. Every substantive recommendation starts with the concrete recommended answer and then gives concise rationale. Use answer-shape guidance only when repository evidence and active project judgment cannot support a substantive recommendation without inventing a user-owned choice or rationale. Never invent the user's rationale. Treat `defer` and `preserve unresolved` as concrete advisory recommendations when evidence is insufficient, and state the material tradeoff between waiting and deciding under uncertainty. Every recommendation remains advisory. The user owns every choice.

End every round with an instantiated concise reply cue using the question numbers in that batch. Use exactly this form: `Reply: 4: <answer>; 5: <answer>. Answer any subset.` Replace `4` and `5` with the batch's actual question numbers, include each batch question once, and do not add other text to the cue. Then wait for the user's reply. Do not answer a user-owned choice on the user's behalf.

Integrate each reply without rehashing settled answers. Preserve each unanswered ready question with its original number. Recompute dependencies after every partial answer, verify new factual claims, check new live approaches, and unlock every newly valid branch. Never repeat a settled question. Ask the next valid batch without asking permission. Do not ask permission to continue.

Press any vague, contradictory, conditional, slogan-like, or incomplete answer with a direct follow-up for the missing condition, threshold, example, rejected path, failure case, consequence, or reversal trigger. Give that follow-up the next unused number.

Continue until the user stops or every material branch has a disposition. If a blocker cannot be resolved from evidence or user judgment, preserve it as an open question instead of guessing.

## Step 6 - Classify the outcome

Before completion, audit the internal coverage ledger for outcome, rationale, constraints, terminology, assumptions, alternatives and rejected paths, consequences, unresolved choices, and material failure cases. In Challenge mode, also audit disconfirming evidence and reversal triggers. Add a prerequisite-ready question for every material gap.

Give every material branch one disposition: settled, verified, rejected with a reason, or explicitly preserved as unresolved. Say `Interview complete` only after every material branch has one of those dispositions. If the user stops early, label the interview incomplete and list only the material unresolved branches.

When the audit passes, render one shared-understanding summary. Classify each outcome into exactly one of these existing record classes:

- **Terminology**: a term and its session meaning. It remains noncanonical unless it expresses a durable, reasoned choice that is separately ratified.
- **Verified current state**: a present-tense fact established by current repository or system evidence. Never place future design here.
- **Open question**: a concrete unresolved ask that matters to later judgment or work.
- **Non-durable detail**: useful session detail that does not belong in project truth.
- **Candidate durable judgment**: a reasoned project choice with constraints, tradeoffs, rejected alternatives, and consequences that may merit a decision.

Render only the nonempty record classes. Include evidence, related decisions, confidence, or uncertainty only when it materially affects the shared understanding. Ask once for correction or confirmation, then end the turn and wait.

Shared understanding is non-authoritative. Confirmation means the summary accurately reflects the interview. It does not approve any Nauro store mutation. Interview agreement never grants write authority.

## Step 7 - Stage writes without authority

Enter this step only if the user explicitly asked to transfer, save, or record the result. Completing the interview does not stage a write. Confirming the shared-understanding summary does not stage a write. Without explicit transfer intent, stop after confirmation.

After both explicit transfer intent and shared-understanding confirmation, determine which classified items could update Nauro. Do not write yet. Keep the three write classes separate:

1. Candidate durable judgment may produce one decision proposal.
2. Verified current state may produce one exact complete state replacement.
3. Open question may produce one exact open-question entry and its exact context.

Terminology and non-durable detail produce no write. Stage one payload at a time. Each write class has a separate later-reply approval gate. Approval for one payload does not authorize another payload or class.

### Decision staging

Before staging a decision, rerun `check_decision` for the final candidate, triage the inline headers, and read every decision used in the operation classification with `get_decision` in full. Classify the proposal as `add`, `update`, or `supersede`.

<!-- protocol:PROPOSE_DECISION_OPERATIONS -->

<!-- protocol:UPDATE_SUPERSEDE_CARE -->

<!-- protocol:NO_INVENT_RATIONALE -->

Determine the project's ratification mode from Nauro before presenting the proposal. Do not infer team mode from a cloud link, repository membership, or the conversation.

Render the complete operation-specific proposal as readable Markdown, including the operation, affected decision when applicable, rationale, rejected alternatives, confidence, type, reversibility, files affected, resolved questions, related decisions, and the agent's overlap assessment. Make the rendered proposal the final text of the turn. Do not call `propose_decision` in that turn.

- **Local or pre-team mode**: before the proposal, tell the user that explicit approval in a later chat reply may authorize the exact `propose_decision` call and commit the decision after validation.
- **Team mode**: before the proposal, tell the user that a later chat reply may authorize submission of the exact pending proposal, but chat approval cannot ratify project judgment. The later first-party ratification step remains mandatory.

### State staging

Determine the project's state-write mode and inspect the active state read and `update_state` schemas before staging a replacement.

- **Local or pre-team mode**: read fresh current state with `get_raw_file(path="state_current.md", project_id=...)` and retain the exact returned content as the staging baseline.
- **Hosted team mode**: read fresh current state through the active hosted state-read surface and retain the exact returned content and exact `state_revision` as the staging baseline. Confirm that the active `update_state` schema accepts `expected_revision`. If the read does not return an exact state revision or the write schema cannot accept it, stop and defer the team-mode state write. Never fall back to an unguarded team-mode state write.

Compose the complete short structured-Markdown replacement that preserves every still-current fact, adds the newly verified state, and removes obsolete state. `update_state` replaces the current narrative with this body and archives the prior body.

Render the exact complete replacement body with its evidence. In hosted team mode, also show the exact baseline `state_revision`. Tell the user that approval must come in a later reply, end the turn, and do not call `update_state` in that turn. A summary confirmation is not state-write approval. The skill must never stage or submit a partial state delta.

### Open-question staging

Render the exact open-question entry and exact context as they would be passed to `flag_question`. Tell the user that approval must come in a later reply, end the turn, and do not call `flag_question` in that turn. A summary confirmation is not question-write approval.

## Step 8 - Recheck, write once, and stop on drift

When the user's later reply explicitly approves the exact staged payload, recheck it before any write or pending submission:

- For a decision, rerun `check_decision`, triage the headers, and read in full every decision used in reasoning. If the related set, status, supersession, operation, or assessment changed, render the revised complete proposal and require fresh approval from another later reply.
- For state in local or pre-team mode, reread `state_current.md` immediately before writing with `get_raw_file(path="state_current.md", project_id=...)` and compare the exact returned content with the staging baseline. Any intervening state change invalidates the approval. Recompose the complete replacement from the new current state, render it, and require fresh approval from another later reply. Re-verify the present-tense facts. If the exact replacement body must change for any other reason, render the changed body and require fresh approval.
- For state in hosted team mode, reread current state through the same hosted state-read surface immediately before writing and compare both the exact content and exact revision with the approved baseline. Any intervening content or revision change invalidates the approval. Recompose the complete replacement from the new current state, render it with the new baseline revision, and require fresh approval from another later reply. Re-verify the present-tense facts. If the exact replacement body must change for any other reason, render the changed body and require fresh approval. If the required revision read or guarded write is unavailable, stop and defer the team-mode state write.
- For an open question, recheck the current open-question record and related decisions. If the exact open-question entry or context must change, render the changed payload and require fresh approval from another later reply.

Changed overlap or changed payload means the prior approval is stale. Never stretch approval to cover a modified write.

If the approved payload remains exact, follow its authority path.

For a decision in local or pre-team mode, make only the approved operation-specific call:

- Add: `propose_decision(project_id=..., title=..., rationale=..., operation="add", rejected=..., confidence=..., decision_type=..., reversibility=..., files_affected=..., resolves_questions=...)`
- Update: `propose_decision(project_id=..., rationale=..., operation="update", affected_decision_id=...)`
- Supersede: `propose_decision(project_id=..., title=..., rationale=..., operation="supersede", affected_decision_id=..., rejected=..., confidence=..., decision_type=..., reversibility=..., files_affected=..., resolves_questions=...)`

In team mode, submit the same exact operation-specific content through `propose_decision` as a pending proposal. Report the returned pending result or secure review link. Canonical ratification requires an authenticated first-party web action bound to the exact proposal revision and digest. Do not claim that the chat reply committed the decision.

For the other write classes, make only the approved call:

- State in local or pre-team mode: `update_state(delta=<exact approved complete replacement body>, project_id=...)`
- State in hosted team mode: call `update_state` with `delta` set to the exact approved complete replacement body, `project_id` set to the resolved project, and `expected_revision` set to the exact approved baseline `state_revision`.
- Question: `flag_question(question=<exact approved question>, context=<exact approved context>, project_id=...)`

Capture and report the tool result. Do not assume that approval means the write succeeded. If another staged item remains, present it through its own separate later-reply approval gate.

## Rules

- The interview runs in the main agent context and waits after every question round.
- The dependency tree is session state, not project truth.
- The coverage ledger stays internal and is not project truth.
- Verify facts from repository or system evidence. Ask the user for judgment and rationale, not discoverable facts.
- Check every live approach against Nauro decisions. Read every decision used in reasoning in full.
- Existing `CONTEXT.md` and ADR content is evidence only. Never edit or generate those files in this workflow.
- Interview completion and summary confirmation do not stage or authorize a write. Staging requires explicit transfer, save, or record intent.
- Shared-understanding confirmation is never write approval.
- Decision proposals, state replacements, and open-question entries use separate exact-payload gates and explicit approval from a later user reply. In team mode, that reply authorizes pending proposal submission only.
- State approval covers one complete replacement body derived from one exact current-state baseline. In hosted team mode, the baseline includes both exact content and exact revision, and the write supplies that revision as `expected_revision`. It never covers a partial delta or a replacement derived after an intervening content or revision change.
- Team-mode project judgment becomes canonical only through authenticated first-party web ratification of the exact proposal revision and digest.
- Recheck before every write or pending submission. Any changed overlap, operation, evidence, or payload requires fresh approval.
- On any tool error, evidence conflict, or surprise, stop and surface it to the user. Do not recover silently.
