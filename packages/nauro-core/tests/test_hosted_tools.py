"""Tests for the hosted-only tool specs and the HOSTED_TOOLS registry.

``update_stack`` and ``share_context`` ship as submodule-public specs for
the hosted HTTP server only: ALL_TOOLS, the stdio registration, the CLI
autogen allowlist, and the public contract snapshot must not move. The
zero-drift pins live in ``test_mcp_tools.py`` (the 11-tool registry) and
the ``packages/nauro`` contract suites; these tests cover the additive
hosted surface.
"""

from __future__ import annotations

import pytest

from nauro_core.mcp_tools import (
    ALL_TOOLS,
    HOSTED_TOOLS,
    SHARE_CONTEXT,
    UPDATE_STACK,
    get_tool_spec,
)

HOSTED_ONLY = (UPDATE_STACK, SHARE_CONTEXT)


class TestHostedRegistry:
    def test_hosted_tools_is_all_tools_plus_the_two_typed_operations(self) -> None:
        assert (*ALL_TOOLS, UPDATE_STACK, SHARE_CONTEXT) == HOSTED_TOOLS

    def test_hosted_only_specs_absent_from_all_tools(self) -> None:
        names = {spec["name"] for spec in ALL_TOOLS}
        assert "update_stack" not in names
        assert "share_context" not in names

    def test_unique_names_across_hosted_registry(self) -> None:
        names = [spec["name"] for spec in HOSTED_TOOLS]
        assert len(names) == len(set(names))

    def test_get_tool_spec_stays_scoped_to_all_tools(self) -> None:
        # The lookup serves the local surfaces; hosted-only specs are
        # reached by direct import until they enter ALL_TOOLS.
        with pytest.raises(KeyError):
            get_tool_spec("update_stack")
        with pytest.raises(KeyError):
            get_tool_spec("share_context")


class TestHostedSpecShape:
    @pytest.mark.parametrize("spec", HOSTED_ONLY, ids=lambda s: s["name"])
    def test_required_fields(self, spec) -> None:
        assert spec["name"]
        assert spec["title"]
        assert spec["description"]
        assert spec["annotations"]
        assert spec["input_schema"]["type"] == "object"

    @pytest.mark.parametrize("spec", HOSTED_ONLY, ids=lambda s: s["name"])
    def test_write_annotations(self, spec) -> None:
        assert spec["annotations"]["readOnlyHint"] is False
        assert spec["annotations"]["destructiveHint"] is False
        assert spec["annotations"]["openWorldHint"] is False
        assert spec["annotations"]["idempotentHint"] is False

    @pytest.mark.parametrize("spec", HOSTED_ONLY, ids=lambda s: s["name"])
    def test_project_id_optional(self, spec) -> None:
        schema = spec["input_schema"]
        assert "project_id" in schema["properties"]
        assert "project_id" not in schema["required"]

    @pytest.mark.parametrize("spec", HOSTED_ONLY, ids=lambda s: s["name"])
    def test_operation_id_schema_optional_with_hosted_required_description(self, spec) -> None:
        # Schema-optional so local adapters can generate one; the
        # description carries the hosted-required contract.
        schema = spec["input_schema"]
        assert "operation_id" in schema["properties"]
        assert "operation_id" not in schema["required"]
        description = schema["properties"]["operation_id"]["description"]
        assert "Required on the hosted HTTP surface" in description


class TestUpdateStackSpec:
    def test_content_is_the_sole_required_property(self) -> None:
        assert UPDATE_STACK["input_schema"]["required"] == ["content"]

    def test_expected_revision_is_optional_string(self) -> None:
        prop = UPDATE_STACK["input_schema"]["properties"]["expected_revision"]
        assert prop["type"] == "string"
        assert "absent" in prop["description"]


class TestShareContextSpec:
    def test_required_properties(self) -> None:
        assert SHARE_CONTEXT["input_schema"]["required"] == [
            "slug",
            "content",
            "pointer_kind",
            "summary",
        ]

    def test_pointer_kind_enum_matches_kernel_vocabulary(self) -> None:
        prop = SHARE_CONTEXT["input_schema"]["properties"]["pointer_kind"]
        assert prop["enum"] == ["brief", "resume", "selection"]
