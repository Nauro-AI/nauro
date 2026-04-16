"""Shared limits, thresholds, valid values, and store filenames.

Canonical definitions for magic numbers and string constants used across
the Nauro ecosystem: token budgets, file names, decision statuses, field
defaults, and similarity thresholds.
"""

# ── L0/L1/L2 payload limits ──
L0_QUESTIONS_LIMIT = 3
L0_DECISIONS_SUMMARY_LIMIT = 10
L1_DECISIONS_LIMIT = 10
L1_DECISIONS_SUMMARY_LIMIT = 10

# ── Validation thresholds ──
VALID_CONFIDENCES: set[str] = {"high", "medium", "low"}
MIN_RATIONALE_LENGTH = 20

# ── Pending confirmation ──
EXPIRY_MINUTES = 10

# ── Decision hash dedup ──
DECISION_HASHES_FILE = ".decision-hashes.json"

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

# ── Decision types ──
DECISION_TYPES: tuple[str, ...] = (
    "architecture",
    "library_choice",
    "pattern",
    "refactor",
    "api_design",
    "infrastructure",
    "data_model",
)

# ── Reversibility levels ──
REVERSIBILITY_LEVELS: tuple[str, ...] = ("easy", "moderate", "hard")

# ── Extraction sources ──
EXTRACTION_SOURCES: tuple[str, ...] = ("compaction", "commit", "manual", "mcp")

# ── Stack empty marker ──
STACK_EMPTY_MARKER = "# Stack\n<!-- Tech choices with rationale and rejected alternatives -->"

# ── Content size limits (H3 — STRIDE) ──
MAX_TITLE_LENGTH = 300
MAX_RATIONALE_LENGTH = 10_000
MAX_DELTA_LENGTH = 5_000
MAX_QUESTION_LENGTH = 2_000
MAX_CONTEXT_LENGTH = 5_000
MAX_APPROACH_LENGTH = 5_000

# ── MCP server instructions ──
# Delivered via the MCP `initialize` response to every connected client.
# Single source of truth — both local (stdio) and remote (HTTP) servers
# should reference this constant.
MCP_INSTRUCTIONS = """\
Nauro is the project's decision memory. Use it to check past decisions \
before committing to an approach, and to record new decisions as you make them.

## When to check decisions

Before adopting any technical approach — architecture, library choice, \
API design, data model, infrastructure — call `check_decision` with a \
description of what you're considering. This includes when the user asks \
"should we...", "what if we...", "can we...", or "check if...". \
The tool surfaces conflicts with past decisions so you don't repeat \
rejected approaches.

## When to propose decisions

Call `propose_decision` when you choose between two or more approaches, \
replace or remove a dependency, establish a new pattern, or cut scope. \
Do it at the moment the decision is made, not at the end of the session. \
Always include what was rejected and why.

Do NOT propose decisions for obvious bug fixes, adding tests for existing \
behavior, or renaming variables.

## When to get context

Call `get_context` at the start of a session or when you need to \
understand the project's current state, goals, and constraints. \
L0 includes the last 10 decisions — do not call `list_decisions` \
after `get_context` unless you need older or superseded decisions.

## When to update state

Call `update_state` when you complete a meaningful unit of work — \
a feature, a refactor, a bug fix — so the next session starts with \
current context.\
"""
