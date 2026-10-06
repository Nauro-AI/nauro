from __future__ import annotations

import hashlib
import json

import pytest
from nauro_core.constants import STACK_NON_AUTHORITATIVE_FRAMING
from nauro_core.operations.update_stack import compute_stack_revision
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.mcp import generation_responses as responses
from nauro.mcp import stdio_server
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID
from tests.test_generation_reads import admitted
from tests.test_generation_responses import fresh_projection
from tests.test_read_dispatch import cloud

__all__ = ["admitted", "cloud"]

STACK = b"# Stack\r\n\r\n- Python \xff\r\n"


@pytest.mark.parametrize("level", ["L0", "L1", "L2"])
def test_context_revisions_use_exact_admitted_bytes(admitted, level):
    binding, current, _ = admitted
    state = b"# State\r\n\r\nCurrent status.\r\n"
    current[0] = fresh_projection({"state_current.md": state, "state.md": b"old"})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_context(binding, level, actor=USER_ID)
    assert result.is_error is False
    assert result.envelope["state_revision"] == hashlib.sha256(state).hexdigest()
    assert f"state_revision: {hashlib.sha256(state).hexdigest()}" in result.text


def test_context_revision_describes_absent_current_state_with_legacy_content(admitted):
    binding, _, _ = admitted
    result = responses.get_context(binding, actor=USER_ID)
    assert result.envelope["state_revision"] == "absent"


@pytest.mark.parametrize(
    "path,field", [("state_current.md", "state_revision"), ("stack.md", "stack_revision")]
)
def test_raw_revision_is_outside_exact_content(admitted, path, field):
    binding, current, _ = admitted
    body = b"# Inventory\r\n\r\nA verified body.\r\n"
    current[0] = fresh_projection({path: body})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_raw_file(binding, path, actor=USER_ID)
    assert result.is_error is False
    assert result.envelope["content"] == body.decode()
    assert result.envelope[field] == hashlib.sha256(body).hexdigest()
    assert f"{field}: {hashlib.sha256(body).hexdigest()}" in result.text
    assert {"state_revision", "stack_revision"} & set(result.envelope) == {field}
    assert (STACK_NON_AUTHORITATIVE_FRAMING in result.text) is (field == "stack_revision")


@pytest.mark.parametrize(
    "path,field", [("state_current.md", "state_revision"), ("stack.md", "stack_revision")]
)
def test_revision_hashes_original_bytes_before_display_replacement(admitted, path, field):
    binding, current, _ = admitted
    body = b"# State\n\nLegacy byte: \xff\n"
    current[0] = fresh_projection({path: body})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_raw_file(binding, path, actor=USER_ID)
    assert result.is_error is False
    assert result.envelope["content"] == body.decode(errors="replace")
    assert result.envelope[field] == hashlib.sha256(body).hexdigest()


def test_stack_revision_line_is_followed_by_non_authoritative_framing(admitted):
    binding, current, _ = admitted
    current[0] = fresh_projection({"stack.md": STACK})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_raw_file(binding, "stack.md", actor=USER_ID)
    revision = compute_stack_revision(STACK)
    authority = result.envelope["read_authority"]
    frame = (
        f"Generation: {authority['generation_id']}. Committed: {authority['committed_at']}.\n"
        "Authorization checked for this read."
    )
    assert result.text == (
        f"stack_revision: {revision}\n{STACK_NON_AUTHORITATIVE_FRAMING}\n\n"
        f"{STACK.decode(errors='replace')}\n\n{frame}"
    )


def test_missing_stack_carries_no_revision(admitted):
    binding, _, _ = admitted
    result = responses.get_raw_file(binding, "stack.md", actor=USER_ID)
    assert result.is_error is True
    assert result.envelope == {
        "store": "local",
        "error": {"kind": "error", "reason": "File not found: stack.md"},
    }
    assert "stack_revision" not in result.text


def test_cli_json_stack_read_carries_revision(cloud):
    binding, current, _ = cloud
    current[0] = fresh_projection({"stack.md": STACK})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = CliRunner().invoke(
        app, ["get-raw-file", "stack.md", "--project", binding.display_name, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["stack_revision"] == compute_stack_revision(STACK)
    assert envelope["content"] == STACK.decode(errors="replace")


def test_stdio_stack_read_carries_revision(cloud):
    binding, current, _ = cloud
    current[0] = fresh_projection({"stack.md": STACK})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = stdio_server.get_raw_file("stack.md", project_id=binding.project_id)
    revision = compute_stack_revision(STACK)
    assert result.isError is False
    assert result.structuredContent["stack_revision"] == revision
    assert result.content[0].text.startswith(
        f"stack_revision: {revision}\n{STACK_NON_AUTHORITATIVE_FRAMING}\n\n"
    )
