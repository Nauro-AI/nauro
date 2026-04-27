"""Tests for the centralized MCP tool registry."""

import pytest

from nauro_core.instructions import (
    MAX_INLINE_PROJECTS,
    WELCOME_NO_PROJECT,
    build_remote_instructions,
)
from nauro_core.mcp_tools import ALL_TOOLS, get_tool_spec

EXPECTED_TOOL_NAMES = {
    "get_context",
    "get_raw_file",
    "list_decisions",
    "get_decision",
    "diff_since_last_session",
    "search_decisions",
    "check_decision",
    "propose_decision",
    "confirm_decision",
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

WRITE_TOOLS = {"propose_decision", "confirm_decision", "flag_question", "update_state"}


class TestRegistry:
    def test_twelve_tools(self):
        assert len(ALL_TOOLS) == 12

    def test_all_expected_names(self):
        names = {spec["name"] for spec in ALL_TOOLS}
        assert names == EXPECTED_TOOL_NAMES

    def test_unique_names(self):
        names = [spec["name"] for spec in ALL_TOOLS]
        assert len(names) == len(set(names))


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
        """Every tool except list_projects must require a `project_id`."""
        props = spec["input_schema"]["properties"]
        required = spec["input_schema"].get("required", [])
        if spec["name"] == "list_projects":
            assert "project_id" not in props
            assert required == []
        else:
            assert "project_id" in props, f"{spec['name']} is missing `project_id`"
            assert "project_id" in required, f"{spec['name']} must require `project_id`"

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


class TestGetContextLevel:
    """Regression: level must be the string enum, not int, to match remote."""

    def test_level_is_string_enum(self):
        spec = get_tool_spec("get_context")
        level = spec["input_schema"]["properties"]["level"]
        assert level["type"] == "string"
        assert level["enum"] == ["L0", "L1", "L2"]


class TestProjectIdRequirement:
    def test_all_tools_require_project_id_except_list_projects(self):
        for spec in ALL_TOOLS:
            required = spec["input_schema"].get("required", [])
            if spec["name"] == "list_projects":
                assert required == [], "list_projects must have no required parameters"
            else:
                assert "project_id" in required, f"{spec['name']} must require project_id"

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

    def test_inline(self):
        projects = [
            {"project_id": "01HZZZZZZZZZZZZZZZZZZZZZZ1", "name": "Beta"},
            {"project_id": "01AAAAAAAAAAAAAAAAAAAAAAA1", "name": "alpha"},
        ]
        result = build_remote_instructions(STATIC, projects)
        assert "Beta" in result
        assert "alpha" in result
        assert "01AAAAAA" in result
        assert "01HZZZZZ" in result
        # alpha sorts before Beta (case-insensitive name)
        assert result.index("alpha") < result.index("Beta")
        # Inline form must NOT mention list_projects (no overflow hint)
        assert "Call list_projects" not in result

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

    def test_static_preserved_verbatim(self):
        projects = [{"project_id": "01ID00000000000000000000A1", "name": "x"}]
        result = build_remote_instructions(STATIC, projects)
        assert STATIC in result
