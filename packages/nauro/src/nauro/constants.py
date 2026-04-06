"""Centralized constants for the Nauro project.

All hardcoded strings for paths, filenames, naming conventions, thresholds,
and parsing patterns live here. Import from this module instead of
scattering magic strings across the codebase.

Constants shared with nauro-core are re-exported from nauro_core.constants.
CLI-specific constants are defined locally.
"""

# ── Re-exports from nauro-core (shared across nauro + mcp-server) ──
from nauro_core.constants import DECISION_HASHES_FILE as DECISION_HASHES_FILE
from nauro_core.constants import DECISION_TYPES as DECISION_TYPES
from nauro_core.constants import DECISIONS_DIR as DECISIONS_DIR
from nauro_core.constants import EXPIRY_MINUTES as EXPIRY_MINUTES
from nauro_core.constants import EXTRACTION_SOURCES as EXTRACTION_SOURCES
from nauro_core.constants import JACCARD_THRESHOLD as JACCARD_THRESHOLD
from nauro_core.constants import L0_DECISIONS_SUMMARY_LIMIT as L0_DECISIONS_SUMMARY_LIMIT
from nauro_core.constants import L0_QUESTIONS_LIMIT as L0_QUESTIONS_LIMIT
from nauro_core.constants import L1_DECISIONS_LIMIT as L1_DECISIONS_LIMIT
from nauro_core.constants import L1_DECISIONS_SUMMARY_LIMIT as L1_DECISIONS_SUMMARY_LIMIT
from nauro_core.constants import MIN_RATIONALE_LENGTH as MIN_RATIONALE_LENGTH
from nauro_core.constants import OPEN_QUESTIONS_MD as OPEN_QUESTIONS_MD
from nauro_core.constants import PROJECT_MD as PROJECT_MD
from nauro_core.constants import REVERSIBILITY_LEVELS as REVERSIBILITY_LEVELS
from nauro_core.constants import SNAPSHOTS_DIR as SNAPSHOTS_DIR
from nauro_core.constants import STACK_EMPTY_MARKER as STACK_EMPTY_MARKER
from nauro_core.constants import STACK_MD as STACK_MD
from nauro_core.constants import STATE_MD as STATE_MD
from nauro_core.constants import VALID_CONFIDENCES as VALID_CONFIDENCES

# ── Environment variables (CLI-specific) ──
NAURO_HOME_ENV = "NAURO_HOME"
NAURO_EXTRACTION_MODEL_ENV = "NAURO_EXTRACTION_MODEL"
NAURO_SIGNAL_THRESHOLD_ENV = "NAURO_SIGNAL_THRESHOLD"

# ── Paths (CLI-specific) ──
DEFAULT_NAURO_HOME = ".nauro"
REGISTRY_FILENAME = "registry.json"
CONFIG_FILENAME = "config.json"
PROJECTS_DIR = "projects"

# Files checked for unfilled bracket prompts during validation
VALIDATED_STORE_FILES = (PROJECT_MD, STATE_MD, STACK_MD)

# Files counted toward L0 token estimate
TOKEN_ESTIMATE_FILES = (PROJECT_MD, STATE_MD, STACK_MD, OPEN_QUESTIONS_MD)

# ── AGENTS.md ──
AGENTS_MD = "AGENTS.md"
CLAUDE_MD = "CLAUDE.md"
NAURO_BLOCK_START = "<!-- NAURO:START — managed by nauro, do not edit -->"
NAURO_BLOCK_END = "<!-- NAURO:END -->"
MANUAL_SECTION_HEADER = "# Manual"

# ── MCP server defaults ──
DEFAULT_MCP_PORT = 7432
MCP_HOST = "127.0.0.1"

# ── Extraction defaults ──
DEFAULT_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_SIGNAL_THRESHOLD = 0.4
EXTRACTION_LOG_FILENAME = "extraction-log.jsonl"

# ── Signal score weights ──
SIGNAL_WEIGHT_ARCHITECTURAL = 0.3
SIGNAL_WEIGHT_NOVELTY = 0.2
SIGNAL_WEIGHT_RATIONALE_DENSITY = 0.2
SIGNAL_WEIGHT_REVERSIBILITY = 0.15
SIGNAL_WEIGHT_SCOPE = 0.15

# ── Validation thresholds (CLI-specific) ──
STALE_SYNC_DAYS = 7
L0_TOKEN_LIMIT = 3500

# ── Token heuristic ──
CHARS_PER_TOKEN = 4  # rough chars-per-token for GPT/Claude family models

# ── Writer limits ──
SLUG_MAX_LENGTH = 60

# ── Snapshot pruning intervals (days) ──
PRUNE_KEEP_ALL_DAYS = 7
PRUNE_DAILY_DAYS = 30
PRUNE_WEEKLY_DAYS = 180

# ── State field patterns (used in parsing and diffing) ──
STATE_FIELD_LAST_SYNCED_BOLD = r"\*\*Last synced:\*\*\s*(.*)"
STATE_FIELD_LAST_SYNCED_ITALIC = r"\*Last synced:\s*(.*?)\*"
STATE_DIFF_FIELDS = ("Sprint", "Focus", "Blockers")

# ── Git hook markers ──
HOOK_START_MARKER = "# --- nauro post-commit hook start ---"
HOOK_END_MARKER = "# --- nauro post-commit hook end ---"

# ── Schema versioning ──
SCHEMA_VERSION = 1
