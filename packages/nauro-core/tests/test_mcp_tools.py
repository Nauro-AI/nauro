"""Tests for the centralized MCP tool registry."""

import pytest

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
}

READ_TOOLS = {
    "get_context",
    "get_raw_file",
    "list_decisions",
    "get_decision",
    "diff_since_last_session",
    "search_decisions",
    "check_decision",
}

WRITE_TOOLS = {"propose_decision", "confirm_decision", "flag_question", "update_state"}


class TestRegistry:
    def test_eleven_tools(self):
        assert len(ALL_TOOLS) == 11

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
    def test_project_param_present(self, spec):
        """Every tool must accept an optional `project` parameter."""
        props = spec["input_schema"]["properties"]
        assert "project" in props, f"{spec['name']} is missing `project`"
        # And it must not be in required — always optional.
        required = spec["input_schema"].get("required", [])
        assert "project" not in required

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
