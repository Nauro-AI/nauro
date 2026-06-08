"""Store reads must tolerate non-UTF-8 bytes.

Store markdown is freeform and hand/agent-editable, so a file saved in a legacy
encoding (a smart quote from a non-UTF-8 editor, an imported cp1252 doc) used to
crash the whole read surface with an unhandled UnicodeDecodeError. Reads now
decode with errors="replace"; these tests pin that the headline read commands,
snapshot capture, and the Store protocol all survive a stray byte.
"""

import json
from pathlib import Path

import pytest

from nauro import constants
from nauro.demo import create_demo_project
from nauro.mcp.tools import tool_get_context, tool_get_raw_file
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.reader import _list_decisions, read_text_lenient
from nauro.store.snapshot import capture_snapshot

# A lone 0xe9 is "é" in latin-1/cp1252 but an invalid UTF-8 start byte.
_BAD_BYTE = b"\xe9"
_REPLACEMENT = "�"


@pytest.fixture()
def poisoned_store(tmp_path: Path) -> Path:
    """A valid demo store with one non-UTF-8 byte in state and in a decision."""
    store_path = tmp_path / "projects" / "demo-project"
    create_demo_project(store_path)

    state = store_path / constants.STATE_CURRENT_FILENAME
    state.write_bytes(b"# Current State\n\nStatus: caf" + _BAD_BYTE + b" shipped\n")

    decision = sorted((store_path / constants.DECISIONS_DIR).glob("*.md"))[0]
    decision.write_bytes(decision.read_bytes() + b"\nlegacy note: na" + _BAD_BYTE + b"ve\n")
    return store_path


def test_read_text_lenient_replaces_bad_byte(poisoned_store):
    state = poisoned_store / constants.STATE_CURRENT_FILENAME
    text = read_text_lenient(state)
    assert _REPLACEMENT in text
    assert "Status: caf" in text


@pytest.mark.parametrize("level", [0, 1, 2])
def test_get_context_survives_non_utf8(poisoned_store, level):
    # The headline read command must not raise on a poisoned store.
    result = tool_get_context(poisoned_store, level)
    assert isinstance(result["content"], str)
    assert result["content"]


def test_list_decisions_survives_non_utf8(poisoned_store):
    decisions = _list_decisions(poisoned_store)
    assert len(decisions) == 7  # all still parse, none crash the read


def test_get_raw_file_survives_non_utf8(poisoned_store):
    # get-raw-file is one of the headline read commands; it must not crash.
    result = tool_get_raw_file(poisoned_store, constants.STATE_CURRENT_FILENAME)
    assert result.get("error") is None
    # ensure_ascii=False keeps the literal U+FFFD so the substring check is valid.
    assert _REPLACEMENT in json.dumps(result, ensure_ascii=False)


def test_filesystem_store_reads_survive_non_utf8(poisoned_store):
    store = FilesystemStore(poisoned_store)
    state = store.read_file(constants.STATE_CURRENT_FILENAME)
    assert state is not None and _REPLACEMENT in state
    for stem in store.list_decisions():
        assert store.read_decision(stem) is not None


def test_snapshot_capture_survives_non_utf8(poisoned_store):
    # sync regenerates AGENTS.md off a snapshot; capture reads every md file.
    version = capture_snapshot(poisoned_store, trigger="test")
    assert version >= 1
