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

from nauro_core.protocol import (
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
        "Optional. If you have one project, the server resolves it "
        "automatically. Pass explicitly if you have multiple — call "
        "list_projects to discover the IDs available to the current user."
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
        "L0 (concise) includes project summary, current state, top open "
        "questions, and the last 10 active decisions with titles and dates. "
        "L1 (working set) adds full decision bodies for recent decisions. "
        "L2 (full dump) includes everything in the store.\n"
        "\n"
        "Call this at session start or when you need to understand the "
        "project's goals, constraints, and recent history. Do NOT call "
        "list_decisions after get_context unless you need decisions beyond "
        "the last 10 or need the include_superseded filter."
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
        "Valid paths include: project.md, state.md, stack.md, open-questions.md, "
        "decisions/042-some-decision.md\n"
        "\n"
        "This is a low-level escape hatch. For most use cases, prefer:\n"
        "- get_context for project overview, state, questions, recent decisions\n"
        "- get_decision for a specific decision by number\n"
        "- search_decisions for finding decisions by topic"
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "File path relative to project store root "
                    "(e.g., 'project.md', 'decisions/001-initial-architecture.md')."
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
        "Browse the full decision history. Use when you need decisions beyond "
        "the last 10 included in get_context, or when you need the "
        "include_superseded filter. For topical lookups, prefer search_decisions."
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
        "Return the full markdown content of a specific decision by its number. "
        "Includes metadata, rationale, and rejected alternatives."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "number": {
                "type": "integer",
                "description": "Decision number (e.g., 23).",
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
        "When days is omitted, diffs the two most recent snapshots "
        "(session-scoped). When days is provided, finds the nearest snapshot "
        "to N days ago and diffs against the current state. Useful for "
        "catching up on changes made in other sessions or on other machines."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": (
                    "Optional: number of days to look back. Finds the nearest "
                    "snapshot to N days ago and diffs against the latest."
                ),
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
        "Search across all project decisions using BM25 relevance ranking "
        "against titles and rationale. Includes both active and superseded "
        "decisions.\n"
        "\n"
        "Use when you need to find decisions about a specific topic rather "
        "than browsing the full list. More token-efficient than list_decisions "
        "for targeted lookups.\n"
        "\n"
        'Example: search_decisions("authentication") returns all decisions '
        "related to auth, OAuth, JWT, etc.\n"
        "\n"
        "Returns decision number, title, date, status, and a relevance "
        "snippet from the matching rationale. Requires a non-empty query."
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
        "WITHOUT writing anything. Returns related decisions (via Tier 1 + "
        "Tier 2 BM25 retrieval) and a deterministic assessment string.\n"
        "\n"
        "This tool does NOT judge conflicts. When the response lists related "
        "decisions, call get_decision on each before proposing — the relevance, "
        "supersession status, and full rationale live in those bodies.\n"
        "\n"
        "Use this to consult the project's decision history before committing "
        'to an approach — especially when the user asks "should we...", '
        '"what if we...", "can we...", or "check if...". If check_decision '
        "returns no related decisions and you want to record the choice, call "
        "propose_decision next with skip_validation=true to avoid redundant "
        "BM25 work."
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
                "description": (
                    "Optional additional context about why you're considering this approach."
                ),
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
        "Propose a new architectural decision for validation and recording.\n"
        "\n"
        "Runs a deterministic validation pipeline before queueing the write:\n"
        "- Tier 1: Structural validation (required fields, length limits)\n"
        "- Tier 2: BM25 similarity check against existing decisions\n"
        "\n"
        "When Tier 2 finds similar decisions, returns status=pending_confirmation "
        "with a confirm_id; the agent must call confirm_decision to commit. When "
        "no similar decisions exist, the write happens immediately.\n"
        "\n"
        "If you already called check_decision for this approach and read the "
        "related decisions, pass skip_validation=true to skip Tier 2. Tier 1 "
        "structural validation always runs.\n"
        "\n"
        "Call this when you choose between two or more approaches, replace or "
        "remove a dependency, establish a new pattern, or cut scope. Always "
        "include what was rejected and why."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the decision.",
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
                "description": (
                    f"How this proposal relates to existing decisions. "
                    f"{PROPOSE_DECISION_OPERATIONS}\n\n"
                    f"{UPDATE_SUPERSEDE_CARE}"
                ),
            },
            "affected_decision_id": {
                "type": "string",
                "description": (
                    "Required when operation is 'update' or 'supersede'. The id "
                    "(e.g. 'decision-042') of the decision being modified."
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
                "description": "Alternatives considered and rejected, each with reason.",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "default": "medium",
                "description": (
                    "Author's confidence in the decision. Use 'high' only "
                    "when a source explicitly accepts or approves the choice; "
                    "'medium' when it is the best available given known "
                    "tradeoffs; 'low' when it is a working assumption that "
                    "may be revisited."
                ),
            },
            "decision_type": {
                "type": "string",
                "enum": [
                    "architecture",
                    "library_choice",
                    "pattern",
                    "refactor",
                    "api_design",
                    "infrastructure",
                    "data_model",
                ],
                "description": (
                    "Optional architectural category for the decision. Helps "
                    "downstream filtering and reporting; omit when none "
                    "applies cleanly."
                ),
            },
            "reversibility": {
                "type": "string",
                "enum": ["easy", "moderate", "hard"],
                "description": (
                    "How costly it would be to reverse this decision later. "
                    "'easy' = config or one-file change; 'moderate' = "
                    "multi-day migration; 'hard' = irreversible without "
                    "significant rework."
                ),
            },
            "files_affected": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of repo-relative paths most affected by "
                    "this decision. Anchors the decision to specific code "
                    "for future reviewers."
                ),
            },
            "resolves_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"{RESOLVES_OPEN_QUESTIONS} Call get_context "
                    "(L0 surfaces the open questions) to discover the ids."
                ),
            },
            "skip_validation": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Skip Tier 2 BM25 matching (Tier 1 always runs). Use when you "
                    "already called check_decision and read the related decisions."
                ),
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["title", "rationale"],
    },
}

CONFIRM_DECISION: ToolSpec = {
    "name": "confirm_decision",
    "title": "Confirm proposed decision",
    "description": (
        "Confirm a previously proposed decision, committing it to the store.\n"
        "\n"
        "Only needed when propose_decision returns status=pending_confirmation. "
        "The confirm_id expires after 10 minutes — if it has expired, call "
        "propose_decision again to get a fresh id. Calling confirm_decision "
        "twice with the same id is safe; only the first call writes."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {
            "confirm_id": {
                "type": "string",
                "description": "The confirm_id returned by propose_decision.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["confirm_id"],
    },
}

FLAG_QUESTION: ToolSpec = {
    "name": "flag_question",
    "title": "Flag open question",
    "description": (
        "Flag an unresolved question for human review. Appends to "
        "open-questions.md in the project store.\n"
        "\n"
        "Before writing, checks whether the question is already addressed by "
        "an existing decision — if so, the response includes a hint pointing "
        "to that decision (the question is still logged).\n"
        "\n"
        "Use when you encounter an ambiguity a human should weigh in on: "
        "architectural trade-offs, unclear requirements, or scope boundaries."
    ),
    "annotations": {**_WRITE_ANNOTATIONS, "idempotentHint": False},
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to flag.",
            },
            "context": {
                "type": "string",
                "description": "Optional context about why this question matters.",
            },
            "project_id": _PROJECT_PARAM,
        },
        "required": ["question"],
    },
}

UPDATE_STATE: ToolSpec = {
    "name": "update_state",
    "title": "Update project state",
    "description": (
        "Update the project's current state with a progress delta and trigger "
        "a snapshot. Before writing, checks for potential contradictions with "
        "recent state entries and returns a warning if found (the update is "
        "still applied).\n"
        "\n"
        "Use when you complete a meaningful unit of work — a feature, a "
        "refactor, a bug fix — so the next session starts with current context."
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
        "Return the projects this user has access to. Other tools auto-resolve "
        "to your project when you have one — call list_projects only if you "
        "have multiple and need to pick a specific project_id to pass."
    ),
    "annotations": {**_READ_ANNOTATIONS, "idempotentHint": True},
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
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
    CONFIRM_DECISION,
    FLAG_QUESTION,
    UPDATE_STATE,
    LIST_PROJECTS,
)

_BY_NAME: dict[str, ToolSpec] = {spec["name"]: spec for spec in ALL_TOOLS}


def get_tool_spec(name: str) -> ToolSpec:
    """Look up a tool spec by name."""
    if name not in _BY_NAME:
        raise KeyError(f"Unknown tool: {name}")
    return _BY_NAME[name]
