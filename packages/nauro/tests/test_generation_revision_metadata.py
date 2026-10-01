from __future__ import annotations

import hashlib

import pytest

from nauro.mcp import generation_responses as responses
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID
from tests.test_generation_reads import admitted
from tests.test_generation_responses import fresh_projection

__all__ = ["admitted"]


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


@pytest.mark.parametrize("path,field", [("state_current.md", "state_revision")])
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


def test_revision_hashes_original_bytes_before_display_replacement(admitted):
    binding, current, _ = admitted
    body = b"# State\n\nLegacy byte: \xff\n"
    current[0] = fresh_projection({"state_current.md": body})
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    result = responses.get_raw_file(binding, "state_current.md", actor=USER_ID)
    assert result.is_error is False
    assert result.envelope["content"] == body.decode(errors="replace")
    assert result.envelope["state_revision"] == hashlib.sha256(body).hexdigest()
