"""Centralized MCP tool metadata — single source of truth for both servers.

Each entry contains the name, human-readable title, description, behavioral
annotations (readOnlyHint, destructiveHint, idempotentHint, openWorldHint),
and JSON Schema for the tool's input. Both the local stdio server and the
remote HTTP server read from this registry so descriptions and annotations
stay in sync.

The local (FastMCP) server derives input schemas from Python type hints, so
it consumes `title`, `description`, and `annotations` here; the function
signature must match `input_schema` by convention.

The remote (JSON-RPC) server consumes the whole entry directly.
"""

from __future__ import annotations

from typing import Any, TypedDict

from nauro_core.constants import (
    MAX_BRIEF_BYTES,
    POINTER_PREFIX_BY_KIND,
    STACK_DOC_CHAR_LIMIT,
    STACK_REVISION_ABSENT,
)
from nauro_core.decision_model import DECISION_TYPE_VALUES
from nauro_core.protocol import (
    _PROPOSAL_VISIBILITY_DETAIL,
    APPROVAL_BEFORE_PROPOSE,
    GET_DECISION_BEFORE_PROPOSING,
    PROPOSE_DECISION_OPERATIONS,
    RESOLVES_OPEN_QUESTIONS,
    UPDATE_SUPERSEDE_CARE,
)


class ToolAnnotations(TypedDict, total=False):
    """Behavioral hints for MCP clients. All fields optional."""

    readOnlyHint: bool
    destructiveHint: bool
    idempotentHint: bool
    openWorldHint: bool


class ToolSpec(TypedDict):
    """Complete metadata for a single MCP tool."""

    name: str
    title: str
    description: str
    annotations: ToolAnnotations
    input_schema: dict[str, Any]


# ── Shared parameter fragments ──

_PROJECT_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional; auto-resolved for one project. With several, list ids "
        "via list_projects (hosted) or `nauro projects` (local)."
    ),
}

# Each ToolSpec selects whether operation_id is required. Existing local-backed
# hosted tools keep it schema-optional for adapter generation; create_project
# requires it.
_OPERATION_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "Client-generated idempotency identifier for this operation (1-128 "
        "printable ASCII characters). Required on the hosted HTTP surface; "
        "resending the same operation_id with the same payload returns the "
        "original outcome instead of repeating the write."
    ),
}

# Every tool is closed-world (operates only on the local/remote Nauro store)
# and non-destructive (writes are additive — nothing is ever deleted).
_READ_ANNOTATIONS: ToolAnnotations = {
    "readOnlyHint": True,
    "openWorldHint": False,
}

_WRITE_ANNOTATIONS: ToolAnnotations = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": False,
}

# ── Tool specs ──

GET_CONTEXT: ToolSpec = {
    "name": "get_context",
    "title": "Get project context",
    "description": (
        "Return project context at the requested detail level.\n"
        "\n"
        "L0 (concise): summary, current state, top open questions, last 10 "
        "active decisions. L1 adds full bodies for recent decisions. L2 "
        "includes everything in the store.\n"
        "\n"
        "Payloads scale with the store: L1 is a bounded working set; L2 can "
        "run past 100k tokens. Default to L0 plus targeted get_decision "
        "lookups.\n"
        "\n"
        "Call at session start. Do NOT call list_decisions after "
        "get_context unless you need decisions beyond the last 10 or the "
        "include_superseded filter."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "enum": ["L0", "L1", "L2"],
                "default": "L0",
                "description": "Detail level: L0 (concise), L1 (working set), L2 (full dump).",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": [],
    },
}

GET_RAW_FILE: ToolSpec = {
    "name": "get_raw_file",
    "title": "Read raw store file",
    "description": (
        "Return the raw markdown content of any file in the Nauro project store.\n"
        "\n"
        "A low-level escape hatch: prefer get_context for overview, "
        "get_decision for one decision, search_decisions for topics. Valid "
        "paths: project.md, state_current.md, stack.md, open-questions.md, "
        "decisions/042-some-decision.md"
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to store root (e.g. 'project.md', "
                    "'decisions/001-initial-architecture.md')."
                ),
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["path"],
    },
}

LIST_DECISIONS: ToolSpec = {
    "name": "list_decisions",
    "title": "List decision history",
    "description": (
        "Browse the full decision history. Use for decisions beyond the last "
        "10 in get_context or for the include_superseded filter; for topical "
        "lookups, prefer search_decisions."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "Maximum decisions to return.",
            },
            "include_superseded": {
                "type": "boolean",
                "default": False,
                "description": "Include superseded decisions in the result.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": [],
    },
}

GET_DECISION: ToolSpec = {
    "name": "get_decision",
    "title": "Get decision by number",
    "description": (
        "Return a specific decision by its number.\n"
        "\n"
        "mode=header returns the triage frontmatter (status, supersession, "
        "date, type, confidence), the title, and a short rationale lede. "
        "mode=full (default) returns the complete markdown. Use header to "
        "triage decisions surfaced by search_decisions or list_decisions "
        "(check_decision hits already carry headers inline), then full for "
        "the ones you actually reason about."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "number": {
                "type": "integer",
                "description": "Decision number (e.g., 23).",
            },
            "mode": {
                "type": "string",
                "enum": ["header", "full"],
                "default": "full",
                "description": "header: triage projection; full: complete body. Default full.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["number"],
    },
}

DIFF_SINCE_LAST_SESSION: ToolSpec = {
    "name": "diff_since_last_session",
    "title": "Diff since last session",
    "description": (
        "Show what changed in the project context since the last snapshot.\n"
        "\n"
        "Omitting days diffs the two most recent snapshots (session-scoped); "
        "with days, diffs the nearest snapshot to N days ago against current "
        "state. Useful for catching up on other sessions or machines."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Optional: days to look back.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": [],
    },
}

SEARCH_DECISIONS: ToolSpec = {
    "name": "search_decisions",
    "title": "Search decisions",
    "description": (
        "Search all project decisions with BM25 ranking against titles and "
        "rationale. Active decisions by default; pass include_superseded=true "
        "to include superseded ones.\n"
        "\n"
        "More token-efficient than list_decisions for topical lookups. "
        "Returns number, title, date, status, and a relevance snippet; the "
        "query must be non-empty."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search text (required, non-empty).",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "Maximum results to return.",
            },
            "include_superseded": {
                "type": "boolean",
                "default": False,
                "description": "Include superseded decisions in the result.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["query"],
    },
}

CHECK_DECISION: ToolSpec = {
    "name": "check_decision",
    "title": "Check decision against existing decisions",
    "description": (
        "Check whether a proposed approach overlaps with existing decisions "
        "WITHOUT writing anything. Returns related decisions via BM25 "
        "keyword retrieval and a deterministic assessment string.\n"
        "\n"
        f"This tool does NOT judge conflicts. {GET_DECISION_BEFORE_PROPOSING}\n"
        "\n"
        "Consult before committing to any approach, especially on "
        '"should we / what if / can we / check if" asks. If no related '
        "decisions return and the choice should be recorded, follow this "
        f"approval boundary before `propose_decision`: {APPROVAL_BEFORE_PROPOSE}"
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "proposed_approach": {
                "type": "string",
                "description": "Description of the approach you're considering.",
            },
            "context": {
                "type": "string",
                "description": "Optional context on why you're considering this approach.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["proposed_approach"],
    },
}

PROPOSE_DECISION: ToolSpec = {
    "name": "propose_decision",
    "title": "Propose a decision",
    "description": (
        "Record an architectural decision. The write commits in a single "
        "call once structural validation passes.\n"
        "\n"
        "Similarity hits are advisory: returned on similar_decisions, never "
        "blocking the write.\n"
        "\n"
        f"{APPROVAL_BEFORE_PROPOSE}\n"
        "\n"
        f"{_PROPOSAL_VISIBILITY_DETAIL}\n"
        "\n"
        "Use for approach choices, dependency swaps, new patterns, and "
        "scope cuts; include what was rejected and why."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "default": "",
                "description": (
                    "Short title for the decision. Required non-empty for add "
                    "and supersede; omit for update, which appends rationale "
                    "only and rejects a non-empty title."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Why this decision was made, including constraints and tradeoffs."
                ),
            },
            "operation": {
                "type": "string",
                "enum": ["add", "update", "supersede"],
                "default": "add",
                "description": (f"{PROPOSE_DECISION_OPERATIONS}\n\n{UPDATE_SUPERSEDE_CARE}"),
            },
            "affected_decision_id": {
                "type": "string",
                "description": (
                    "Required for update and supersede: the id "
                    "(e.g. 'decision-042') being modified."
                ),
            },
            "rejected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "alternative": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
                "description": (
                    "Alternatives considered and rejected. Each item needs a "
                    "non-empty 'alternative' key (legacy alias: 'name') plus "
                    "a 'reason'."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                # No schema default: the adapter applies "medium" for add/
                # supersede when confidence is unset, and relies on an unset
                # (None) value to recognise "caller did not send confidence" on
                # update. A schema default makes every derived transport (the
                # CLI autogen surface) materialise "medium", which the kernel
                # then rejects as a disallowed metadata change on update.
                "description": (
                    "Author's confidence: 'high' only with explicit source "
                    "approval; 'medium' best available; 'low' working "
                    "assumption."
                ),
            },
            "decision_type": {
                "type": "string",
                "enum": list(DECISION_TYPE_VALUES),
                "description": (
                    "Optional architectural category; omit when none applies cleanly."
                ),
            },
            "reversibility": {
                "type": "string",
                "enum": ["easy", "moderate", "hard"],
                "description": (
                    "Cost to reverse later: 'easy' = config or one-file "
                    "change; 'moderate' = multi-day migration; 'hard' = "
                    "major rework."
                ),
            },
            "files_affected": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional repo-relative paths most affected, anchoring the decision to code."
                ),
            },
            "resolves_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (f"{RESOLVES_OPEN_QUESTIONS} get_context L0 lists the ids."),
            },
            "project_id": _PROJECT_PARAM,
        },
        # title is optional: operation="update" appends rationale only and the
        # kernel rejects a non-empty title on update, so a required title made
        # update uncallable through any schema-respecting transport. On add/
        # supersede the kernel still rejects an empty title structurally.
        "required": ["rationale"],
    },
}

FLAG_QUESTION: ToolSpec = {
    "name": "flag_question",
    "title": "Flag or resolve open question",
    "description": (
        "Flag an unresolved question for human review, or resolve existing "
        "questions against a decision. Writes to open-questions.md in the "
        "project store.\n"
        "\n"
        "To flag: pass `question`; if an existing decision already addresses "
        "it, the response hints at it (the question is still logged).\n"
        "\n"
        "To resolve: pass `resolved_by` and the entry ids in `targets`; each "
        "is stamped resolved and, when prose-safe, moved below "
        "`## Resolved`.\n"
        "\n"
        "Before resolving, read the cited decision. Verify that it is active "
        "and answers each question's operative ask; shared terms or topic "
        "overlap are not sufficient. A bulk close-out requires a complete "
        "question census and the recorded session-decision procedure.\n"
        "\n"
        "Pass exactly one of `question` or `resolved_by`."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to flag; omit when passing resolved_by.",
            },
            "context": {
                "type": "string",
                "description": "Optional context about why this question matters.",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Question ids (Q### or legacy timestamp form). On flag: "
                    "optional duplicate candidates - if any is already "
                    "resolved, the call short-circuits without appending. On "
                    "resolve: the entries to stamp; every id must exist."
                ),
            },
            "resolved_by": {
                "type": "string",
                "description": (
                    "Decision id (e.g. D123) resolving the entries in "
                    "targets; when set, the call resolves instead of "
                    "appending."
                ),
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": [],
    },
}

UPDATE_STATE: ToolSpec = {
    "name": "update_state",
    "title": "Update project state",
    "description": (
        "Update the project's current state with a progress delta and trigger "
        "a snapshot.\n"
        "\n"
        "If the delta appears to repeat recent state entries, a warning "
        "returns (the update still applies). Use when you complete a "
        "meaningful unit of work so the next session starts with current "
        "context."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "delta": {
                "type": "string",
                "description": 'Description of what changed (e.g. "Deployed v0.2.0 to staging").',
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["delta"],
    },
}

# list_projects is the discovery entry point. Other tools resolve the user's
# project automatically when only one exists; agents only need to call
# list_projects when they have multiple projects and must disambiguate.
LIST_PROJECTS: ToolSpec = {
    "name": "list_projects",
    "title": "List projects",
    "description": (
        "Return the projects this user has access to. Call only when you "
        "have multiple projects and need a project_id to pass; other tools "
        "auto-resolve when you have one."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

# ── Hosted-only tool specs ──
#
# These specs are served by the hosted HTTP surface only: they are deliberately
# absent from ALL_TOOLS, which the stdio server, the CLI autogen allowlist, and
# the public contract snapshot all key off, so defining them here changes no
# local surface.

UPDATE_STACK: ToolSpec = {
    "name": "update_stack",
    "title": "Update the stack document",
    "description": (
        "Replace the complete stack.md document in one call. content is the "
        f"full replacement (Markdown, at most {STACK_DOC_CHAR_LIMIT:,} "
        "characters); partial patches are not supported.\n"
        "\n"
        "Pass expected_revision — the stack_revision from a prior read, or "
        f"'{STACK_REVISION_ABSENT}' when creating a missing file — to make "
        "the write conditional: a mismatch returns stale_revision with the "
        "current revision and writes nothing.\n"
        "\n"
        "stack.md is reported material: record technologies, dependencies, "
        "and compatibility observations. Goals, constraints, and choices "
        "with rationale belong in propose_decision, not here."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The complete replacement stack.md document; stored "
                    "byte-exact with no normalization."
                ),
            },
            "expected_revision": {
                "type": "string",
                "description": (
                    "Optional precondition: the lowercase SHA-256 hex "
                    "stack_revision observed on a prior read, or "
                    f"'{STACK_REVISION_ABSENT}' when the file did not exist. "
                    "If supplied it is enforced."
                ),
            },
            "operation_id": _OPERATION_ID_PARAM,
            "project_id": _PROJECT_PARAM,
        },
        "required": ["content"],
    },
}

SHARE_CONTEXT: ToolSpec = {
    "name": "share_context",
    "title": "Share a context brief",
    "description": (
        "Create one immutable context brief and its discovery pointer as a "
        "single operation: the body lands at context/<slug>.md and a "
        "pointer entry is appended to open-questions.md so other agents "
        "can find it.\n"
        "\n"
        "Slugs are permanent — a taken slug returns slug_conflict with a "
        "fresh suggestion; retry as a new operation. Use pointer_kind "
        "'brief' for shared context, 'resume' for in-flight work handoffs, "
        "'selection' for parked candidate sets."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "Brief name: 1-120 lowercase ASCII letters, digits, or "
                    "internal hyphens. Maps only to context/<slug>.md."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    f"The complete immutable brief body (at most {MAX_BRIEF_BYTES:,} UTF-8 bytes)."
                ),
            },
            "pointer_kind": {
                "type": "string",
                "enum": list(POINTER_PREFIX_BY_KIND),
                "description": (
                    "Discovery-pointer kind: brief (shared context), resume "
                    "(in-flight work handoff), or selection (parked "
                    "candidate set)."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Nonempty single line (at most 300 characters) with at "
                    "least one visible character, carried in the discovery "
                    "pointer; match the brief's own summary."
                ),
            },
            "operation_id": _OPERATION_ID_PARAM,
            "project_id": _PROJECT_PARAM,
        },
        "required": ["slug", "content", "pointer_kind", "summary"],
    },
}

CREATE_PROJECT: ToolSpec = {
    "name": "create_project",
    "title": "Create a project",
    "description": (
        "Create an empty hosted project for the authenticated account. This tool works "
        "before the caller has any project.\n\n"
        "Pass only name and operation_id. The server trims outer whitespace from name. "
        "The result must be nonempty and at most 100 Python characters. No other character "
        "restrictions apply.\n\n"
        "The authenticated bearer identity becomes the creator and initial owner. The server "
        "derives the project ID, membership, owner role, timestamps, generation identity, "
        "storage keys, format and epoch facts, neutral alias, receipt, digest, audit event, "
        "counter, and transaction facts. The caller cannot supply any of them.\n\n"
        "operation_id defines an account-scoped idempotency key. Resending the same operation_id "
        "with the same trimmed name replays the original outcome instead of creating another "
        "project. A different trimmed name conflicts."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Project name. Outer whitespace is trimmed; the result must be nonempty and "
                    "at most 100 Python characters. No other character restrictions apply."
                ),
            },
            "operation_id": _OPERATION_ID_PARAM,
        },
        "required": ["name", "operation_id"],
        "additionalProperties": False,
    },
}

# ── Registry ──

ALL_TOOLS: tuple[ToolSpec, ...] = (
    GET_CONTEXT,
    GET_RAW_FILE,
    LIST_DECISIONS,
    GET_DECISION,
    DIFF_SINCE_LAST_SESSION,
    SEARCH_DECISIONS,
    CHECK_DECISION,
    PROPOSE_DECISION,
    FLAG_QUESTION,
    UPDATE_STATE,
    LIST_PROJECTS,
)

# The hosted HTTP server's registry: every shared tool plus the
# hosted-only typed operations above. Local surfaces keep keying off
# ALL_TOOLS, so this tuple is inert for them.
HOSTED_TOOLS: tuple[ToolSpec, ...] = (
    *ALL_TOOLS,
    UPDATE_STACK,
    SHARE_CONTEXT,
    CREATE_PROJECT,
)

_BY_NAME: dict[str, ToolSpec] = {spec["name"]: spec for spec in ALL_TOOLS}


def get_tool_spec(name: str) -> ToolSpec:
    """Look up a tool spec by name."""
    if name not in _BY_NAME:
        raise KeyError(f"Unknown tool: {name}")
    return _BY_NAME[name]
