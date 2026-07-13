"""Tests for the centralized MCP tool registry."""

import pytest

from nauro_core.constants import MCP_INSTRUCTIONS_STATIC
from nauro_core.instructions import (
    MAX_INLINE_PROJECTS,
    WELCOME_NO_PROJECT,
    build_remote_instructions,
)
from nauro_core.mcp_tools import ALL_TOOLS, get_tool_spec
from nauro_core.protocol import _APPROVAL_BEFORE_PROPOSE

# Real-shaped 26-char ULIDs for the inline-rendering tests.
ULID_ALPHA = "01AAAAAAAAAAAAAAAAAAAAAAAA"
ULID_BETA = "01HZZZZZZZZZZZZZZZZZZZZZZZ"

EXPECTED_TOOL_NAMES = {
    "get_context",
    "get_raw_file",
    "list_decisions",
    "get_decision",
    "diff_since_last_session",
    "search_decisions",
    "check_decision",
    "propose_decision",
    "flag_question",
    "update_state",
    "list_projects",
}

READ_TOOLS = {
    "get_context",
    "get_raw_file",
    "list_decisions",
    "get_decision",
    "diff_since_last_session",
    "search_decisions",
    "check_decision",
    "list_projects",
}

WRITE_TOOLS = {"propose_decision", "flag_question", "update_state"}


class TestRegistry:
    def test_eleven_tools(self):
        assert len(ALL_TOOLS) == 11

    def test_all_expected_names(self):
        names = {spec["name"] for spec in ALL_TOOLS}
        assert names == EXPECTED_TOOL_NAMES

    def test_unique_names(self):
        names = [spec["name"] for spec in ALL_TOOLS]
        assert len(names) == len(set(names))

    def test_confirm_decision_not_in_registry(self):
        """Sentinel: confirm_decision was removed with the trust-model
        relocation. The single-call propose_decision flow has no
        separate confirm tool."""
        names = {spec["name"] for spec in ALL_TOOLS}
        assert "confirm_decision" not in names


class TestSpecShape:
    @pytest.mark.parametrize("spec", ALL_TOOLS, ids=lambda s: s["name"])
    def test_required_fields(self, spec):
        assert spec["name"]
        assert spec["title"]
        assert spec["description"]
        assert spec["annotations"]
        assert spec["input_schema"]

    @pytest.mark.parametrize("spec", ALL_TOOLS, ids=lambda s: s["name"])
    def test_input_schema_is_object(self, spec):
        assert spec["input_schema"]["type"] == "object"
        assert "properties" in spec["input_schema"]

    @pytest.mark.parametrize("spec", ALL_TOOLS, ids=lambda s: s["name"])
    def test_project_id_param(self, spec):
        """Every non-list_projects tool exposes an OPTIONAL `project_id`.

        The server resolves the user's project automatically when only one
        exists; the parameter is for explicit selection in the multi-project
        case. It must therefore be in `properties` (so agents can pass it)
        but never in `required`.
        """
        props = spec["input_schema"]["properties"]
        required = spec["input_schema"].get("required", [])
        if spec["name"] == "list_projects":
            assert "project_id" not in props
            assert required == []
        else:
            assert "project_id" in props, f"{spec['name']} is missing `project_id`"
            assert "project_id" not in required, (
                f"{spec['name']} must NOT require `project_id` — "
                "the server auto-resolves single-project users."
            )

    @pytest.mark.parametrize("spec", ALL_TOOLS, ids=lambda s: s["name"])
    def test_closed_world(self, spec):
        """All Nauro tools operate on the local/remote store, not the open web."""
        assert spec["annotations"].get("openWorldHint") is False


class TestReadWriteAnnotations:
    @pytest.mark.parametrize("name", sorted(READ_TOOLS))
    def test_read_tools_are_readonly(self, name):
        spec = get_tool_spec(name)
        assert spec["annotations"].get("readOnlyHint") is True

    @pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
    def test_write_tools_not_readonly(self, name):
        spec = get_tool_spec(name)
        assert spec["annotations"].get("readOnlyHint") is False

    @pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
    def test_write_tools_not_destructive(self, name):
        """Nauro writes are additive — no tool ever deletes data."""
        spec = get_tool_spec(name)
        assert spec["annotations"].get("destructiveHint") is False


class TestLookup:
    def test_get_tool_spec_by_name(self):
        spec = get_tool_spec("check_decision")
        assert spec["name"] == "check_decision"

    def test_get_tool_spec_unknown_raises(self):
        with pytest.raises(KeyError):
            get_tool_spec("not_a_tool")

    @pytest.mark.parametrize("name", ["check_decision", "propose_decision"])
    def test_decision_write_guidance_carries_approval_boundary(self, name: str) -> None:
        assert _APPROVAL_BEFORE_PROPOSE in get_tool_spec(name)["description"]


class TestGetContextLevel:
    """Regression: level must be the string enum, not int, to match remote."""

    def test_level_is_string_enum(self):
        spec = get_tool_spec("get_context")
        level = spec["input_schema"]["properties"]["level"]
        assert level["type"] == "string"
        assert level["enum"] == ["L0", "L1", "L2"]


class TestProjectIdOptional:
    def test_no_tool_requires_project_id(self):
        """Server-side default resolution makes project_id optional everywhere."""
        for spec in ALL_TOOLS:
            required = spec["input_schema"].get("required", [])
            assert "project_id" not in required, f"{spec['name']} must NOT require project_id"

    def test_param_description_mentions_auto_resolve(self):
        """_PROJECT_PARAM description must signal that the server resolves."""
        # Pull from any non-list_projects tool — they all share _PROJECT_PARAM.
        spec = get_tool_spec("check_decision")
        desc = spec["input_schema"]["properties"]["project_id"]["description"]
        assert "Optional" in desc
        assert "resolves" in desc or "auto-resolve" in desc

    def test_list_projects_in_all_tools(self):
        names = {spec["name"] for spec in ALL_TOOLS}
        assert "list_projects" in names
        spec = get_tool_spec("list_projects")
        schema = spec["input_schema"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema.get("required", []) == []


STATIC = "STATIC_BLOCK"


class TestBuildRemoteInstructions:
    def test_zero_projects(self):
        result = build_remote_instructions(STATIC, [])
        assert STATIC in result
        assert WELCOME_NO_PROJECT in result

    def test_zero_projects_welcome_before_static(self):
        """The per-user section must precede the static block so it survives
        client-side truncation of ``initialize.instructions``."""
        result = build_remote_instructions(STATIC, [])
        assert result.index(WELCOME_NO_PROJECT) < result.index(STATIC)

    def test_one_project_orientation_only(self):
        """Single-project users get a name-only orientation line — no ULID.

        Auto-resolve handles dispatch, so rendering the project_id is just
        noise (and was the source of the historical truncation bug).
        """
        projects = [{"project_id": ULID_ALPHA, "name": "nauro"}]
        result = build_remote_instructions(STATIC, projects)
        assert STATIC in result
        assert "nauro" in result, "project name must appear for orientation"
        assert ULID_ALPHA not in result, "single-project rendering must NOT include the ULID"
        assert "auto-resolve" in result or "automatically" in result or "auto" in result
        # The old "Pass the matching project_id" directive is gone.
        assert "Pass the matching project_id" not in result

    def test_one_project_orientation_before_static(self):
        """Orientation line must precede the static block."""
        projects = [{"project_id": ULID_ALPHA, "name": "nauro"}]
        result = build_remote_instructions(STATIC, projects)
        assert result.index("Connected to project") < result.index(STATIC)

    def test_two_projects_emits_full_ulids(self):
        """Regression: the multi-project branch renders full 26-char ULIDs.

        Previously it emitted `project_id[:8]` for every project (including
        the single-project case) — the truncated form failed server-side
        ULID validation. Full ULIDs are unambiguous.
        """
        projects = [
            {"project_id": ULID_BETA, "name": "Beta"},
            {"project_id": ULID_ALPHA, "name": "alpha"},
        ]
        result = build_remote_instructions(STATIC, projects)
        assert "Beta" in result
        assert "alpha" in result
        assert ULID_ALPHA in result, "full alpha ULID must be present"
        assert ULID_BETA in result, "full beta ULID must be present"
        # alpha sorts before Beta (case-insensitive name)
        assert result.index("alpha") < result.index("Beta")
        # Inline form must NOT mention list_projects (no overflow hint)
        assert "Call list_projects" not in result

    def test_two_projects_list_before_static(self):
        """The inline project list must precede the static block."""
        projects = [
            {"project_id": ULID_ALPHA, "name": "alpha"},
            {"project_id": ULID_BETA, "name": "Beta"},
        ]
        result = build_remote_instructions(STATIC, projects)
        # "You have N projects" is the heading line that opens the section.
        assert result.index("You have 2 projects") < result.index(STATIC)

    def test_two_projects_directive_requires_explicit_id(self):
        """Multi-project rendering must tell the agent disambiguation is required."""
        projects = [
            {"project_id": ULID_ALPHA, "name": "alpha"},
            {"project_id": ULID_BETA, "name": "beta"},
        ]
        result = build_remote_instructions(STATIC, projects)
        assert "explicit project_id" in result
        assert "Pass the matching project_id" not in result

    def test_overflow(self):
        projects = [
            {"project_id": f"01ID{i:022d}", "name": f"proj-{i}"}
            for i in range(MAX_INLINE_PROJECTS + 2)
        ]
        result = build_remote_instructions(STATIC, projects)
        assert STATIC in result
        assert str(len(projects)) in result
        assert "list_projects" in result
        # Names must NOT be enumerated in overflow mode
        for p in projects:
            assert p["name"] not in result

    def test_overflow_pointer_before_static(self):
        """The overflow pointer must precede the static block."""
        projects = [
            {"project_id": f"01ID{i:022d}", "name": f"proj-{i}"}
            for i in range(MAX_INLINE_PROJECTS + 2)
        ]
        result = build_remote_instructions(STATIC, projects)
        # The "You have N projects." count line opens the overflow section.
        assert result.index(f"You have {len(projects)} projects") < result.index(STATIC)

    def test_static_preserved_verbatim(self):
        projects = [{"project_id": "01ID00000000000000000000A1", "name": "x"}]
        result = build_remote_instructions(STATIC, projects)
        assert STATIC in result


# The claude.ai client truncates the MCP initialize.instructions field at
# roughly this many characters; tools/list descriptions arrive intact even
# when it is truncated, which is why per-tool guidance is the durable home.
INSTRUCTIONS_TRUNCATION_LIMIT = 2023

# Representative multi-project inputs that stress the prepended per-user
# section the way real callers do (long names, full ULIDs).
THREE_PROJECTS = [
    {"project_id": "01KQ6AZGNA0B3QBF67NBXP3S45", "name": "nauro"},
    {"project_id": "01KREWKMPDW2EVR66F9XXNERGB", "name": "throwaway-supersede-1778616226"},
    {"project_id": "01KRVJWEPXJHTC7ZZNHVAR0PV5", "name": "valid-empty-1779042236"},
]
ONE_PROJECT = [{"project_id": "01KQ6AZGNA0B3QBF67NBXP3S45", "name": "nauro"}]

# Pathological but server-legal inputs: 100 characters is the server-side
# project-name cap (no nauro-core constant exists for it), paired with full
# 26-char ULIDs.
MAX_NAME_PROJECTS = [
    {"project_id": "01KQ6AZGNA0B3QBF67NBXP3S45", "name": "a" * 100},
    {"project_id": "01KREWKMPDW2EVR66F9XXNERGB", "name": "b" * 100},
    {"project_id": "01KRVJWEPXJHTC7ZZNHVAR0PV5", "name": "c" * 100},
]

SECTION_HEADERS = (
    "## When to check decisions",
    "## When to propose decisions",
    "## When to get context",
)


class TestInstructionsSurviveTruncation:
    """Tiered contract for what survives the claude.ai client's truncation
    of the composed ``initialize.instructions`` payload.

    The per-user section is the load-bearing payload: it carries the
    project_id a multi-project caller must pass, and no other surface
    delivers it. Static-block guidance has a durable home on the matching
    ToolSpec descriptions, which tools/list delivers intact, so losing the
    static tail is recoverable — losing the per-user section is not.

    - Tier 1 (full survival): 0- and 1-project compositions fit entirely
      under the cliff.
    - Tier 2 (header + per-user survival): a realistic inline multi-project
      composition keeps every static section header and the whole per-user
      section before the cliff; static tail-body truncation is accepted.
    - Tier 3 (per-user survival): a pathological max-name composition
      guarantees only the per-user section; static headers may truncate.
    """

    @pytest.mark.parametrize("projects", [[], ONE_PROJECT], ids=["zero", "one"])
    def test_tier1_full_composition_survives(self, projects):
        result = build_remote_instructions(MCP_INSTRUCTIONS_STATIC, projects)
        assert len(result) <= INSTRUCTIONS_TRUNCATION_LIMIT, (
            f"composed instructions are {len(result)} chars, past the "
            f"{INSTRUCTIONS_TRUNCATION_LIMIT}-char truncation point"
        )

    def test_tier2_every_section_header_before_truncation(self):
        result = build_remote_instructions(MCP_INSTRUCTIONS_STATIC, THREE_PROJECTS)
        for header in SECTION_HEADERS:
            offset = result.find(header)
            assert offset != -1, f"{header} missing from composed instructions"
            assert offset < INSTRUCTIONS_TRUNCATION_LIMIT, (
                f"{header} falls at offset {offset}, past the "
                f"{INSTRUCTIONS_TRUNCATION_LIMIT}-char truncation point"
            )

    def test_tier2_per_user_section_before_truncation(self):
        """The entire per-user section (project list plus the explicit
        project_id directive) must end before the cliff — the static block
        may lose tail body here, but never the per-user payload."""
        result = build_remote_instructions(MCP_INSTRUCTIONS_STATIC, THREE_PROJECTS)
        static_offset = result.index(MCP_INSTRUCTIONS_STATIC)
        assert static_offset < INSTRUCTIONS_TRUNCATION_LIMIT, (
            f"static block starts at offset {static_offset}, past the "
            f"{INSTRUCTIONS_TRUNCATION_LIMIT}-char truncation point"
        )

    def test_tier3_per_user_section_before_truncation_max_names(self):
        """Accepted limit: with server-cap-length names, static section
        headers may fall past the cliff — only the per-user section (project
        list plus the explicit-project_id directive) is guaranteed."""
        result = build_remote_instructions(MCP_INSTRUCTIONS_STATIC, MAX_NAME_PROJECTS)
        static_offset = result.index(MCP_INSTRUCTIONS_STATIC)
        assert static_offset < INSTRUCTIONS_TRUNCATION_LIMIT, (
            f"static block starts at offset {static_offset}, past the "
            f"{INSTRUCTIONS_TRUNCATION_LIMIT}-char truncation point"
        )


class TestTrimmedGuidanceCanonicalHome:
    """Guidance trimmed from the static block must remain reachable on the
    matching ToolSpec descriptions, which tools/list delivers intact."""

    def test_update_state_description_carries_completion_guidance(self):
        """The 'When to update state' static section was trimmed; the
        update_state ToolSpec description is its canonical home."""
        desc = get_tool_spec("update_state")["description"]
        assert "meaningful unit of work" in desc
        assert "next session starts with current context" in desc

    def test_get_context_description_carries_list_decisions_nuance(self):
        """The 'do not call list_decisions after get_context' nuance was the
        truncation-risk tail of the static get-context section; the
        get_context ToolSpec description is its canonical home."""
        desc = get_tool_spec("get_context")["description"]
        assert "list_decisions" in desc
        assert "after get_context" in desc

    def test_get_context_description_carries_scale_guidance(self):
        """Payload sizes scale with the store, and only the get_context
        description says so. A rewrite that drops the guidance re-invites
        full-dump L2 calls against mature stores."""
        desc = get_tool_spec("get_context")["description"]
        assert "bounded working set" in desc
        assert "scale with the store" in desc
