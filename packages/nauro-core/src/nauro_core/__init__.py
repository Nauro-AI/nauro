"""nauro-core: shared pure-Python logic for the Nauro ecosystem.

Re-export facade — public API symbols from all modules.
"""

__version__ = "0.3.0"

from nauro_core.constants import (
    DECISION_HASHES_FILE as DECISION_HASHES_FILE,
)
from nauro_core.constants import (
    DECISION_TYPES as DECISION_TYPES,
)
from nauro_core.constants import (
    DECISIONS_DIR as DECISIONS_DIR,
)
from nauro_core.constants import (
    EXPIRY_MINUTES as EXPIRY_MINUTES,
)
from nauro_core.constants import (
    EXTRACTION_SOURCES as EXTRACTION_SOURCES,
)
from nauro_core.constants import (
    L0_DECISIONS_SUMMARY_LIMIT as L0_DECISIONS_SUMMARY_LIMIT,
)
from nauro_core.constants import (
    L0_QUESTIONS_LIMIT as L0_QUESTIONS_LIMIT,
)
from nauro_core.constants import (
    L1_DECISIONS_LIMIT as L1_DECISIONS_LIMIT,
)
from nauro_core.constants import (
    L1_DECISIONS_SUMMARY_LIMIT as L1_DECISIONS_SUMMARY_LIMIT,
)
from nauro_core.constants import (
    MAX_APPROACH_LENGTH as MAX_APPROACH_LENGTH,
)
from nauro_core.constants import (
    MAX_CONTEXT_LENGTH as MAX_CONTEXT_LENGTH,
)
from nauro_core.constants import (
    MAX_DELTA_LENGTH as MAX_DELTA_LENGTH,
)
from nauro_core.constants import (
    MAX_QUESTION_LENGTH as MAX_QUESTION_LENGTH,
)
from nauro_core.constants import (
    MAX_RATIONALE_LENGTH as MAX_RATIONALE_LENGTH,
)
from nauro_core.constants import (
    MAX_TITLE_LENGTH as MAX_TITLE_LENGTH,
)
from nauro_core.constants import (
    MCP_INSTRUCTIONS as MCP_INSTRUCTIONS,
)
from nauro_core.constants import (
    MCP_INSTRUCTIONS_STATIC as MCP_INSTRUCTIONS_STATIC,
)
from nauro_core.constants import (
    MIN_RATIONALE_LENGTH as MIN_RATIONALE_LENGTH,
)
from nauro_core.constants import (
    OPEN_QUESTIONS_MD as OPEN_QUESTIONS_MD,
)
from nauro_core.constants import (
    PROJECT_MD as PROJECT_MD,
)
from nauro_core.constants import (
    REVERSIBILITY_LEVELS as REVERSIBILITY_LEVELS,
)
from nauro_core.constants import (
    SNAPSHOTS_DIR as SNAPSHOTS_DIR,
)
from nauro_core.constants import (
    STACK_EMPTY_MARKER as STACK_EMPTY_MARKER,
)
from nauro_core.constants import (
    STACK_MD as STACK_MD,
)
from nauro_core.constants import (
    STATE_CURRENT_FILENAME as STATE_CURRENT_FILENAME,
)
from nauro_core.constants import (
    STATE_HISTORY_FILENAME as STATE_HISTORY_FILENAME,
)
from nauro_core.constants import (
    STATE_LEGACY_FILENAME as STATE_LEGACY_FILENAME,
)
from nauro_core.constants import (
    STATE_MD as STATE_MD,
)
from nauro_core.constants import (
    VALID_CONFIDENCES as VALID_CONFIDENCES,
)
from nauro_core.context import (
    build_l0 as build_l0,
)
from nauro_core.context import (
    build_l1 as build_l1,
)
from nauro_core.context import (
    build_l2 as build_l2,
)
from nauro_core.instructions import (
    MAX_INLINE_PROJECTS as MAX_INLINE_PROJECTS,
)
from nauro_core.instructions import (
    WELCOME_NO_PROJECT as WELCOME_NO_PROJECT,
)
from nauro_core.instructions import (
    build_remote_instructions as build_remote_instructions,
)
from nauro_core.decision_model import (
    Decision as Decision,
)
from nauro_core.decision_model import (
    DecisionConfidence as DecisionConfidence,
)
from nauro_core.decision_model import (
    DecisionSource as DecisionSource,
)
from nauro_core.decision_model import (
    DecisionStatus as DecisionStatus,
)
from nauro_core.decision_model import (
    DecisionType as DecisionType,
)
from nauro_core.decision_model import (
    RejectedAlternative as RejectedAlternative,
)
from nauro_core.decision_model import (
    Reversibility as Reversibility,
)
from nauro_core.decision_model import (
    format_decision_v2 as format_decision_v2,
)
from nauro_core.decision_model import (
    parse_decision_v2 as parse_decision_v2,
)
from nauro_core.mcp_tools import (
    ALL_TOOLS as ALL_TOOLS,
)
from nauro_core.mcp_tools import (
    ToolSpec as ToolSpec,
)
from nauro_core.mcp_tools import (
    get_tool_spec as get_tool_spec,
)
from nauro_core.parsing import (
    decisions_summary_lines as decisions_summary_lines,
)
from nauro_core.parsing import (
    extract_current_state as extract_current_state,
)
from nauro_core.parsing import (
    extract_decision_number as extract_decision_number,
)
from nauro_core.parsing import (
    extract_relevance_snippet as extract_relevance_snippet,
)
from nauro_core.parsing import (
    extract_stack_oneliner as extract_stack_oneliner,
)
from nauro_core.parsing import (
    extract_stack_summary as extract_stack_summary,
)
from nauro_core.parsing import (
    parse_decision as parse_decision,
)
from nauro_core.parsing import (
    parse_questions as parse_questions,
)
from nauro_core.pending import (
    PendingStore as PendingStore,
)
from nauro_core.search import (
    bm25_retrieve as bm25_retrieve,
)
from nauro_core.search import (
    bm25_search as bm25_search,
)
from nauro_core.state import (
    StateUpdateResult as StateUpdateResult,
)
from nauro_core.state import (
    assemble_state_for_context as assemble_state_for_context,
)
from nauro_core.state import (
    migrate_legacy_state as migrate_legacy_state,
)
from nauro_core.state import (
    prepare_state_update as prepare_state_update,
)
from nauro_core.validation import (
    TIER2_STOPWORDS as TIER2_STOPWORDS,
)
from nauro_core.validation import (
    TIER2_TOP_K as TIER2_TOP_K,
)
from nauro_core.validation import (
    check_bm25_similarity as check_bm25_similarity,
)
from nauro_core.validation import (
    check_content_length as check_content_length,
)
from nauro_core.validation import (
    compute_hash as compute_hash,
)
from nauro_core.validation import (
    screen_structural as screen_structural,
)
