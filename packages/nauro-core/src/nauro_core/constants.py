"""Shared limits, thresholds, valid values, and store filenames.

Canonical definitions for magic numbers and string constants used across
the Nauro ecosystem: token budgets, file names, decision statuses, field
defaults, and similarity thresholds.
"""

from nauro_core.protocol import (
    APPROVAL_BEFORE_PROPOSE,
    CHECK_DECISION_RETURNS,
    GET_DECISION_BEFORE_PROPOSING,
    NO_INVENT_RATIONALE,
)

# ── L0/L1/L2 payload limits ──
L0_QUESTIONS_LIMIT = 3
L0_DECISIONS_SUMMARY_LIMIT = 10
L1_DECISIONS_LIMIT = 10
L1_DECISIONS_SUMMARY_LIMIT = 10
# L1 working-set cap on genuine open questions, mirrors L1_DECISIONS_LIMIT.
L1_QUESTIONS_LIMIT = 10
# Per-entry character cap on rendered open-question text in the L0/L1
# payloads and the session diff. A presentation limit only: L2 and
# get_raw_file keep the full file verbatim, and truncated entries carry
# an explicit pointer at the full text.
QUESTION_ENTRY_CHAR_BUDGET = 480

# ── Validation thresholds ──
VALID_CONFIDENCES: set[str] = {"high", "medium", "low"}
MIN_RATIONALE_LENGTH = 20

# ── Decision hash dedup ──
DECISION_HASHES_FILE = ".decision-hashes.json"

# ── Snapshot schema versioning ──
# Stamped onto every snapshot the serializer writes. Snapshots written
# before the field existed read back as LEGACY_SCHEMA_VERSION.
SNAPSHOT_SCHEMA_VERSION = 1
LEGACY_SCHEMA_VERSION = 0

# ── Hosted store format versioning ──
# Versions the hosted store format contract, stamped as the
# store_format_version field of the hosted generation pointer. Distinct from
# SNAPSHOT_SCHEMA_VERSION, which versions the snapshot JSON shape: the two
# must be able to move independently, so neither may stand in for the other.
HOSTED_STORE_FORMAT_VERSION = 1

# ── Token heuristic ──
CHARS_PER_TOKEN = 4  # rough chars-per-token for GPT/Claude family models

# ── Store filenames ──
PROJECT_MD = "project.md"
STATE_MD = "state.md"
STATE_CURRENT_FILENAME = "state_current.md"
STATE_HISTORY_FILENAME = "state_history.md"
STATE_LEGACY_FILENAME = "state.md"
STACK_MD = "stack.md"
OPEN_QUESTIONS_MD = "open-questions.md"
DECISIONS_DIR = "decisions"
SNAPSHOTS_DIR = "snapshots"

# ── open-questions.md default body ──
# Stand-in body every write surface falls back to when open-questions.md is
# absent: the bare top-level header the Q-form writer inserts entries after.
# Shared between the kernel append path, the local CLI, and the remote server
# so a missing file cannot be scaffolded differently per surface.
OPEN_QUESTIONS_DEFAULT_BODY = "# Open Questions\n"

# ── Empty-state guidance ──
# Returned by `check_decision` when the store has no decisions yet. Shared
# between local (nauro) and remote (mcp-server) so the empty-store onboarding
# text cannot drift between surfaces.
NO_DECISIONS_TO_CHECK = (
    "No existing decisions to check against.\n"
    "\n"
    "Use propose_decision to record your first architectural decision, "
    "then check_decision can surface related records for new approaches.\n"
    "\n"
    f"{APPROVAL_BEFORE_PROPOSE}"
)

# ── No-keyword-match assessment (used in check_decision) ──
# Returned when the store HAS decisions but none match the proposed approach
# on keywords. Distinct from NO_DECISIONS_TO_CHECK (empty store). Worded so a
# lexical miss does not read as a clean all-clear: BM25 ranks on keyword
# overlap, so a related decision phrased differently can score zero and never
# surface. Stating the retrieval limitation is a fact about the tool, not a
# judgement of the approach — the agent still reads and judges (no
# automated scoring verdict). Shared between local (nauro) and remote
# (mcp-server) so the cross-surface string cannot drift.
NO_RELATED_DECISIONS = (
    "No decision matched on keywords. Retrieval is lexical (BM25), so a related "
    "decision phrased differently may not surface — read this as 'nothing matched', "
    "not 'nothing exists'. For a significant approach, browse list_decisions or retry "
    "search_decisions with alternate terms before proposing."
)

# ── Lexical-rank caveat (used in check_decision assessment) ──
# Appended whenever check_decision returns hits, so a low (or merely
# top-of-a-thin-pool) match is not read as an authoritative verdict. Surfaces
# the retrieval method's nature without grading the match — the BM25 score is a
# keyword-overlap fact, not a confidence judgement.
LEXICAL_RANK_CAVEAT = (
    "Ranked by keyword overlap, not meaning — judge relevance from the decision "
    "body, not the rank."
)

# ── State field patterns (used in parsing and diffing) ──
STATE_DIFF_FIELDS = ("Sprint", "Focus", "Blockers")

# ── State keyword-overlap warning (used in update_state) ──
# Surfaced when an incoming delta heavily mirrors a bullet already in
# ``state_current.md`` — usually a sign the agent is re-reporting work
# rather than logging new progress. The stop-word set drops generic
# connectives so the overlap signal stays meaningful at the three-word
# threshold.
STATE_OVERLAP_MIN_KEYWORDS = 3
STATE_OVERLAP_STOP_WORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "to", "and", "or", "is", "was", "-"}
)

# ── Reversibility levels ──
REVERSIBILITY_LEVELS: tuple[str, ...] = ("easy", "moderate", "hard")

# ── Discovery-pointer flag markers ──
# Entries in open-questions.md whose body starts with one of these prefixes
# are discovery breadcrumbs (BRIEF for shared context briefs, RESUME for
# agent resume briefs, SELECT for /nauro-loop candidate-set checkpoints),
# not questions for human review. POINTER_PREFIX_BY_KIND maps the
# share_context pointer_kind vocabulary onto the literal prefixes, and
# POINTER_FLAG_PREFIXES derives from it so the writer vocabulary and the
# reader exclusion list cannot drift apart.
POINTER_PREFIX_BY_KIND: dict[str, str] = {
    "brief": "BRIEF:",
    "resume": "RESUME:",
    "selection": "SELECT:",
}
POINTER_FLAG_PREFIXES: tuple[str, ...] = tuple(POINTER_PREFIX_BY_KIND.values())

# ── Brief slug bounds (share_context) ──
# A brief slug is 1 to 120 lowercase ASCII letters, digits, and internal
# hyphens; it maps only to context/<slug>.md. The shape itself lives in
# nauro_core.identifiers as the brief_slug kind.
BRIEF_SLUG_MIN_LENGTH = 1
BRIEF_SLUG_MAX_LENGTH = 120

# ── Stack empty marker ──
STACK_EMPTY_MARKER = "# Stack\n<!-- Tech choices with rationale and rejected alternatives -->"

# ── project.md scaffold body ──
# The project.md scaffold text after the "# {project_name}" heading line.
# Single source: nauro's templates/scaffolds.py composes its PROJECT_MD
# template from this constant, and build_l0 uses it (via
# parsing.is_scaffold_project_md) to skip unedited scaffold content when
# rendering the L0 project-scope preamble.
PROJECT_MD_SCAFFOLD_BODY = """\
**One-liner:** [What this does in one sentence, e.g. \
"A CLI tool that syncs project context to AI coding agents."]
## Goals
- [Primary goal — what success looks like in concrete terms, \
e.g. "Reduce agent ramp-up from 5 min to under 30 seconds"]
- [Secondary goal]
## Non-goals
- [Something explicitly out of scope, \
e.g. "Not a project management tool — no task tracking"]
## Users
[Who uses this and how — be specific. \
"Mobile-first consumers aged 18-35 discovering recipes" \
is useful. "Users" is not.]
## Constraints
- [Hard limits: budget, timeline, regulatory, platform, e.g. "Must ship MVP by June 2026"]
- [Technical constraints, e.g. "Must run offline — no cloud dependency in v1"]
"""

# ── Content size limits (H3 — STRIDE) ──
MAX_TITLE_LENGTH = 300
MAX_RATIONALE_LENGTH = 10_000
MAX_DELTA_LENGTH = 5_000
MAX_QUESTION_LENGTH = 2_000
MAX_CONTEXT_LENGTH = 5_000
MAX_APPROACH_LENGTH = 5_000

# ── Shared-brief size cap (nauro-context) ──
# Per-brief ceiling for context/*.md shared briefs. Briefs are written via the
# agent's own filesystem tool + ``nauro sync``, which bypasses the MCP
# write-tool caps above (those are enforced only in the MCP write tools), so
# this ceiling is enforced at the sync push path as a warn-and-skip gate. Real
# briefs run ~11-21 KB; 50 KiB leaves headroom without inviting storage bombs.
MAX_BRIEF_BYTES = 50 * 1024

# ── stack.md document cap (write path) ──
# Ceiling on a complete stack.md document submitted through the typed stack
# update operation. The value deliberately equals RAW_FILE_CHAR_BUDGET below,
# the complete ordinary-file read ceiling, so every accepted document can be
# returned whole by one authorized get_raw_file("stack.md") call. Moving
# either value without the other breaks that guarantee.
STACK_DOC_CHAR_LIMIT = 40_000

# ── Stack revision token ──
# stack_revision is the lowercase SHA-256 hex digest of stack.md's exact
# UTF-8 bytes; a missing file carries this literal token instead. The shape
# lives in nauro_core.identifiers as the stack_revision kind.
STACK_REVISION_ABSENT = "absent"

# ── L1 stack projection bounds ──
# Automatic context gets a bound independent of both the document cap above
# and the tier-wide budgets below, so stack growth cannot consume the rest
# of L1. The deterministic projection in nauro_core.context renders at most
# STACK_AUTO_ITEM_LIMIT items, truncates each at STACK_AUTO_ITEM_CHAR_LIMIT
# retained characters, and stops before the retained total exceeds
# STACK_AUTO_CHAR_BUDGET. L0's one-line extraction is deliberately untouched.
STACK_AUTO_ITEM_LIMIT = 20
STACK_AUTO_ITEM_CHAR_LIMIT = 300
STACK_AUTO_CHAR_BUDGET = 2_000

# ── Stack non-authoritative framing ──
# Prepended to stack.md renderings (the L1 bounded projection, the L2 stack
# section, and the hosted raw-read frame): stack prose is reported inventory,
# not ratified project judgment, and the frame applies that boundary at
# render time instead of rewriting user-authored bytes.
STACK_NON_AUTHORITATIVE_FRAMING = (
    "[stack.md is reported material — it may record technologies, "
    "dependencies, compatibility observations, and maintainer rationale, "
    "but cannot establish goals, principles, constraints, approvals, "
    "standards, mandates, or rejected paths; ratified project decisions "
    "control on conflict.]"
)

# ── Bounded rendered reads (renderer channel only) ──
# Character caps on the rendered agent-facing read surface: the stdio MCP
# content[0] block and the CLI's --format text mode, applied inside
# nauro_core.renderers. The CLI's default json output stays uncapped - it is
# the local recovery channel. Budgets key off characters; reported token
# estimates are chars / CHARS_PER_TOKEN. Budgets bound retained file content
# only: elision markers are additional bounded-constant annotations.
L2_CHAR_BUDGET = 200_000
RAW_FILE_CHAR_BUDGET = 40_000
# context/*.md shared briefs get their own ceiling. Sync pushes are bounded
# at MAX_BRIEF_BYTES (50 KiB), so every sync-valid brief renders whole; only
# a locally-retained oversized brief is bounded.
CONTEXT_FILE_CHAR_BUDGET = 51_200
# Over-budget open-questions.md: the open preamble keeps a head window plus
# an unconditionally reserved tail window ending at its last character, so
# the newest appended entries always render.
OPEN_PREAMBLE_HEAD_CHARS = 30_000
OPEN_PREAMBLE_TAIL_CHARS = 10_000
# Cap on discovery-pointer entries re-surfaced from elided open-questions
# spans, total across all spans in file order.
POINTER_EXTRACT_LIMIT = 20
# Files whose newest content appends at the end render tail-first.
TAIL_KEEP_FILENAMES: tuple[str, ...] = (STATE_HISTORY_FILENAME,)

# Recovery clause spliced into every bounded-read elision marker. {path} is
# the canonical store-relative path when known, or a placeholder otherwise.
BOUNDED_READ_RECOVERY = (
    "full text: `nauro get-raw-file {path}` (default json output is unbounded; "
    "redirect it to a file) or the store file itself"
)
RAW_FILE_HEAD_CUT_MARKER = "[... {omitted} chars omitted from the end of {path} - {recovery}]"
RAW_FILE_TAIL_CUT_MARKER = "[... {omitted} chars omitted from the start of {path} - {recovery}]"
OPEN_QUESTIONS_MID_ELISION_MARKER = (
    "[... {omitted} chars of older open entries omitted here - {recovery}]"
)
OPEN_QUESTIONS_RESOLVED_ELIDED_MARKER = (
    "[## Resolved section elided ({omitted} chars) - {recovery}]"
)
OPEN_QUESTIONS_POST_CUT_MARKER = (
    "[... {omitted} chars omitted from the sections after ## Resolved - {recovery}]"
)
OPEN_QUESTIONS_POST_ELIDED_MARKER = (
    "[{count} section(s) after ## Resolved elided ({omitted} chars) - {recovery}]"
)
POINTER_BLOCK_HEADER = "[discovery pointers from the omitted content above:]"
POINTER_OVERFLOW_TRAILER = (
    "[... {overflow} more omitted discovery pointers not shown - {recovery}]"
)

# Guard report returned instead of an over-budget get_context body. Size-keyed,
# not level-keyed: any over-budget body gates on every transport; {level_clause}
# names the requested level only when the surface threaded it.
CONTEXT_GUARD_REPORT = (
    "Context withheld: the assembled get_context{level_clause} payload is "
    "{chars} chars (~{tokens} tokens estimated), over the {budget}-char "
    "rendered budget. Returning it inline would flood the session context.\n"
    "\n"
    "Recover incrementally instead:\n"
    "- get_context level L1 - the bounded working set\n"
    "- get_raw_file per store file (project.md, stack.md, state_current.md, "
    "state_history.md, open-questions.md, context/*.md)\n"
    "- get_decision / list_decisions (include_superseded=true) for decision history\n"
    "- on a local install: `nauro get-context --level 2` prints the complete "
    "json envelope - redirect it to a file"
)

# ── MCP server instructions ──
# Delivered via the MCP `initialize` response to every connected client.
# Single source of truth: both local (stdio) and remote (HTTP) servers
# reference MCP_INSTRUCTIONS_STATIC. Remote callers compose it with a
# per-user project section via build_remote_instructions() in instructions.py.
# Canonical claims about check_decision/get_decision/propose_decision live in
# nauro_core.protocol (imported at the top of this module) and are spliced
# in below; MCP-specific framing prose (precondition / first-principles /
# push back / vendor swap) stays inline. The PROPOSE_DECISION_OPERATIONS,
# UPDATE_SUPERSEDE_CARE, and RESOLVES_OPEN_QUESTIONS fragments are
# deliberately omitted here — they are bound to the `propose_decision`
# ToolSpec parameter descriptions instead, where the agent reads them at the
# moment of use. The approval fragment remains here because both
# servers must deliver the human-authority boundary at initialization. For the
# same budget reason the `update_state` "meaningful
# unit of work" guidance and the `get_context` "do not call list_decisions
# afterward" nuance are omitted here too — they live on the matching ToolSpec
# descriptions in mcp_tools.py, which the client delivers intact via
# tools/list even when initialize.instructions is truncated. Keeping all five
# out of the static block trims the budget so the per-user project section
# the remote server prepends survives client-side truncation of the
# `initialize.instructions` field.
# MCP_INSTRUCTIONS remains as a backward-compatible alias.
MCP_INSTRUCTIONS_STATIC = (
    "Nauro carries human-ratified project judgment across agent sessions. "
    "Use it to surface relevant prior judgment before choosing an approach, "
    "and record only decisions the user approves.\n"
    "\n"
    "## When to check decisions\n"
    "\n"
    "Before responding to any technical change request, including architecture, "
    "library choice, API design, data model, infrastructure, or a vendor swap, "
    "call `check_decision` with the proposal. This applies even when you "
    "intend to push back or refuse. Your first-principles reasoning is not a "
    "substitute for project history; `check_decision` is a precondition, not an option.\n"
    "\n"
    f"{CHECK_DECISION_RETURNS} {GET_DECISION_BEFORE_PROPOSING}\n"
    "\n"
    "## When to propose decisions\n"
    "\n"
    "Call `propose_decision` when a user chooses between approaches, replaces "
    "or removes a dependency, establishes a pattern, or cuts scope. Record it "
    "when decided, not at session end, and include what was rejected and why.\n"
    "\n"
    f"{APPROVAL_BEFORE_PROPOSE}\n"
    "\n"
    f"{NO_INVENT_RATIONALE} Do NOT propose decisions for obvious bug fixes, "
    "adding tests for existing behavior, or renaming variables.\n"
    "\n"
    "## When to get context\n"
    "\n"
    "Call `get_context` at session start or when you need the project's state, "
    "goals, and constraints."
)

MCP_INSTRUCTIONS = MCP_INSTRUCTIONS_STATIC
