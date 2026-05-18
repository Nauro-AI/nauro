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
from nauro_core.constants import STATE_CURRENT_FILENAME as STATE_CURRENT_FILENAME
from nauro_core.constants import STATE_HISTORY_FILENAME as STATE_HISTORY_FILENAME
from nauro_core.constants import STATE_LEGACY_FILENAME as STATE_LEGACY_FILENAME
from nauro_core.constants import STATE_MD as STATE_MD
from nauro_core.constants import VALID_CONFIDENCES as VALID_CONFIDENCES

# ── Environment variables (CLI-specific) ──
NAURO_HOME_ENV = "NAURO_HOME"
NAURO_TELEMETRY_ENV = "NAURO_TELEMETRY"

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

# Files counted toward L0 token estimate
TOKEN_ESTIMATE_FILES = (
    PROJECT_MD,
    STATE_CURRENT_FILENAME,
    STATE_LEGACY_FILENAME,
    STACK_MD,
    OPEN_QUESTIONS_MD,
)

# ── AGENTS.md ──
AGENTS_MD = "AGENTS.md"
CLAUDE_MD = "CLAUDE.md"
NAURO_BLOCK_START = "<!-- NAURO:START — managed by nauro, do not edit -->"
NAURO_BLOCK_END = "<!-- NAURO:END -->"
MANUAL_SECTION_HEADER = "# Manual"
SKILLS_SECTION_HEADER = "## Skills"

# ── MCP server defaults ──
DEFAULT_MCP_PORT = 7432
MCP_HOST = "127.0.0.1"

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
STATE_DIFF_FIELDS = ("Sprint", "Focus", "Blockers")

# ── Schema versioning ──
SCHEMA_VERSION = 1

# ── Telemetry ──
TELEMETRY_CONSENT_VERSION = 1

# ── Repo-local config (.nauro/config.json inside each repo) ──
REPO_CONFIG_DIR = ".nauro"
REPO_CONFIG_FILENAME = "config.json"
REPO_CONFIG_SCHEMA_VERSION = 1
REPO_CONFIG_MODE_LOCAL = "local"
REPO_CONFIG_MODE_CLOUD = "cloud"

# ── Registry schema versions ──
REGISTRY_SCHEMA_VERSION_V1 = 1
REGISTRY_SCHEMA_VERSION_V2 = 2
