"""Shared limits, thresholds, valid values, and store filenames.

Canonical definitions for magic numbers and string constants used across
the Nauro ecosystem: token budgets, file names, decision statuses, field
defaults, and similarity thresholds.
"""

from nauro_core.protocol import (
    CHECK_DECISION_RETURNS,
    GET_DECISION_BEFORE_PROPOSING,
    NO_INVENT_RATIONALE,
    UPDATE_SUPERSEDE_CARE,
)

# ── L0/L1/L2 payload limits ──
L0_QUESTIONS_LIMIT = 3
L0_DECISIONS_SUMMARY_LIMIT = 10
L1_DECISIONS_LIMIT = 10
L1_DECISIONS_SUMMARY_LIMIT = 10

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

# ── Empty-state guidance ──
# Returned by `check_decision` when the store has no decisions yet. Shared
# between local (nauro) and remote (mcp-server) so the empty-store onboarding
# text cannot drift between surfaces.
NO_DECISIONS_TO_CHECK = (
    "No existing decisions to check against.\n"
    "\n"
    "Use propose_decision to record your first architectural decision, "
    "then check_decision can help verify new approaches against "
    "your recorded decisions."
)

# ── No-keyword-match assessment (used in check_decision) ──
# Returned when the store HAS decisions but none match the proposed approach
# on keywords. Distinct from NO_DECISIONS_TO_CHECK (empty store). Worded so a
# lexical miss does not read as a clean all-clear: BM25 ranks on keyword
# overlap, so a related decision phrased differently can score zero and never
# surface. Stating the retrieval limitation is a fact about the tool, not a
# judgement of the approach — the agent still reads and judges (D130/D245: no
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
# keyword-overlap fact, not a confidence judgement (D130/D245).
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
# agent resume briefs), not questions for human review.
POINTER_FLAG_PREFIXES: tuple[str, ...] = ("BRIEF:", "RESUME:")

# ── Stack empty marker ──
STACK_EMPTY_MARKER = "# Stack\n<!-- Tech choices with rationale and rejected alternatives -->"

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

# ── MCP server instructions ──
# Delivered via the MCP `initialize` response to every connected client.
# Single source of truth — both local (stdio) and remote (HTTP) servers
# reference MCP_INSTRUCTIONS_STATIC. Remote callers compose it with a
# per-user project section via build_remote_instructions() in instructions.py.
# Canonical claims about check_decision/get_decision/propose_decision live in
# nauro_core.protocol (imported at the top of this module) and are spliced
# in below; MCP-specific framing prose (precondition / first-principles /
# push back / vendor swap) stays inline. The PROPOSE_DECISION_OPERATIONS and
# RESOLVES_OPEN_QUESTIONS fragments are deliberately omitted here — they are
# bound to the `propose_decision` ToolSpec parameter descriptions instead,
# where the agent reads them at the moment of use. For the same budget reason
# the `update_state` "meaningful unit of work" guidance and the `get_context`
# "do not call list_decisions afterward" nuance are omitted here too — they
# live on the matching ToolSpec descriptions in mcp_tools.py, which the client
# delivers intact via tools/list even when initialize.instructions is
# truncated. Keeping all four out of the static block trims the budget so the
# per-user project section the remote server prepends survives client-side
# truncation of the `initialize.instructions` field.
# MCP_INSTRUCTIONS remains as a backward-compatible alias.
MCP_INSTRUCTIONS_STATIC = (
    "Nauro carries this project's doctrine across every agent session. "
    "Use it to check past decisions before adopting an approach, and to "
    "record new decisions as you make them.\n"
    "\n"
    "## When to check decisions\n"
    "\n"
    "Before responding to any technical change request — architecture, "
    "library choice, API design, data model, infrastructure, vendor swap — "
    "call `check_decision` with a description of what's being proposed. "
    'This includes "should we...", "what if we...", "can we...", '
    '"check if..." framings, and applies even when you intend to push back '
    "or refuse. Your first-principles reasoning is not a substitute for "
    "project history; `check_decision` is a precondition, not an option.\n"
    "\n"
    f"{CHECK_DECISION_RETURNS} {GET_DECISION_BEFORE_PROPOSING}\n"
    "\n"
    "## When to propose decisions\n"
    "\n"
    "Call `propose_decision` when you choose between two or more approaches, "
    "replace or remove a dependency, establish a new pattern, or cut scope. "
    "Do it at the moment the decision is made, not at the end of the "
    "session. Always include what was rejected and why.\n"
    "\n"
    f"You own this classification. {UPDATE_SUPERSEDE_CARE}\n"
    "\n"
    f"{NO_INVENT_RATIONALE} Do NOT propose decisions for obvious bug fixes, "
    "adding tests for existing behavior, or renaming variables.\n"
    "\n"
    "## When to get context\n"
    "\n"
    "Call `get_context` at the start of a session or when you need to "
    "understand the project's current state, goals, and constraints."
)

MCP_INSTRUCTIONS = MCP_INSTRUCTIONS_STATIC
