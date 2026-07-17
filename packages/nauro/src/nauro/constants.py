"""Centralized constants for the Nauro project.

All hardcoded strings for paths, filenames, naming conventions, thresholds,
and parsing patterns live here. Import from this module instead of
scattering magic strings across the codebase.

Constants shared with nauro-core are re-exported from nauro_core.constants.
CLI-specific constants are defined locally.
"""

# ── Re-exports from nauro-core (shared across nauro + mcp-server) ──
# DECISION_TYPES comes from the public facade because its canonical source is
# the DecisionType enum in nauro_core.decision_model, not nauro_core.constants.
from nauro_core import DECISION_TYPES as DECISION_TYPES
from nauro_core.constants import CHARS_PER_TOKEN as CHARS_PER_TOKEN
from nauro_core.constants import DECISION_HASHES_FILE as DECISION_HASHES_FILE
from nauro_core.constants import DECISIONS_DIR as DECISIONS_DIR
from nauro_core.constants import L0_DECISIONS_SUMMARY_LIMIT as L0_DECISIONS_SUMMARY_LIMIT
from nauro_core.constants import L0_QUESTIONS_LIMIT as L0_QUESTIONS_LIMIT
from nauro_core.constants import L1_DECISIONS_LIMIT as L1_DECISIONS_LIMIT
from nauro_core.constants import L1_DECISIONS_SUMMARY_LIMIT as L1_DECISIONS_SUMMARY_LIMIT
from nauro_core.constants import MIN_RATIONALE_LENGTH as MIN_RATIONALE_LENGTH
from nauro_core.constants import OPEN_QUESTIONS_MD as OPEN_QUESTIONS_MD
from nauro_core.constants import POINTER_FLAG_PREFIXES as POINTER_FLAG_PREFIXES
from nauro_core.constants import PROJECT_MD as PROJECT_MD
from nauro_core.constants import REVERSIBILITY_LEVELS as REVERSIBILITY_LEVELS
from nauro_core.constants import SNAPSHOT_SCHEMA_VERSION as SNAPSHOT_SCHEMA_VERSION
from nauro_core.constants import SNAPSHOTS_DIR as SNAPSHOTS_DIR
from nauro_core.constants import STACK_EMPTY_MARKER as STACK_EMPTY_MARKER
from nauro_core.constants import STACK_MD as STACK_MD
from nauro_core.constants import STATE_CURRENT_FILENAME as STATE_CURRENT_FILENAME
from nauro_core.constants import STATE_HISTORY_FILENAME as STATE_HISTORY_FILENAME
from nauro_core.constants import STATE_LEGACY_FILENAME as STATE_LEGACY_FILENAME
from nauro_core.constants import STATE_MD as STATE_MD
from nauro_core.constants import VALID_CONFIDENCES as VALID_CONFIDENCES

# ── Environment variables (CLI-specific) ──
NAURO_HOME_ENV = "NAURO_HOME"
NAURO_EMBEDDINGS_ENV = "NAURO_EMBEDDINGS"

# ── Paths (CLI-specific) ──
DEFAULT_NAURO_HOME = ".nauro"
REGISTRY_FILENAME = "registry.json"
CONFIG_FILENAME = "config.json"
PROJECTS_DIR = "projects"

# Files checked for unfilled bracket prompts during validation. Includes
# both state filenames so validation works on fresh stores
# (state_current.md) and legacy stores (state.md until the first
# update_state migrates them). Missing files are skipped by the validator,
# so listing both is safe.
VALIDATED_STORE_FILES = (PROJECT_MD, STATE_CURRENT_FILENAME, STATE_LEGACY_FILENAME, STACK_MD)

# ── AGENTS.md ──
AGENTS_MD = "AGENTS.md"
CLAUDE_MD = "CLAUDE.md"
NAURO_BLOCK_START = "<!-- NAURO:START — managed by nauro, do not edit -->"
NAURO_BLOCK_END = "<!-- NAURO:END -->"
MANUAL_SECTION_HEADER = "# Manual"
SKILLS_SECTION_HEADER = "## Skills"

# ── Validation thresholds (CLI-specific) ──
STALE_SYNC_DAYS = 7
L0_TOKEN_LIMIT = 3500
# project.md leads every L0 payload as the stable-scope preamble, so its size
# lands on every session start across every surface. 2,000 estimated tokens
# (8 KB at CHARS_PER_TOKEN = 4) is several times a healthy scope file; beyond
# that the detail belongs in stack.md or decisions.
PROJECT_MD_TOKEN_WARN = 2_000

# ── flag_question similar-decision hint ──
# Compared against a raw BM25 score (not a normalized 0-1 similarity); above
# this the flag is annotated with a hint pointing at the matching decision.
FLAG_QUESTION_HINT_MIN_SCORE = 0.7
FLAG_QUESTION_HINT_TITLE_LENGTH = 100

# ── Snapshot pruning intervals (days) ──
PRUNE_KEEP_ALL_DAYS = 7
PRUNE_DAILY_DAYS = 30
PRUNE_WEEKLY_DAYS = 180

# ── Repo-local config (.nauro/config.json inside each repo) ──
REPO_CONFIG_DIR = ".nauro"
REPO_CONFIG_FILENAME = "config.json"
REPO_CONFIG_SCHEMA_VERSION = 1
REPO_CONFIG_MODE_LOCAL = "local"
REPO_CONFIG_MODE_CLOUD = "cloud"

# ── Registry schema versions ──
REGISTRY_SCHEMA_VERSION_V1 = 1
REGISTRY_SCHEMA_VERSION_V2 = 2
