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
from nauro.cli.commands.import_cmd import _append_to_store_file
from nauro.demo import create_demo_project
from nauro.mcp.tools import tool_get_context, tool_get_raw_file
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.reader import _list_decisions, read_text_lenient
from nauro.store.registry import register_project
from nauro.store.snapshot import capture_snapshot
from nauro.templates.agents_md import (
    parse_preserved_sections,
    regenerate_agents_md_for_project,
)
from nauro.templates.scaffolds import scaffold_project_store

# A lone 0xe9 is "é" in latin-1/cp1252 but an invalid UTF-8 start byte.
_BAD_BYTE = b"\xe9"
_REPLACEMENT = "�"
# Load-bearing non-ASCII the templates actually emit: em-dash (U+2014) is in
# the scaffold decision body and the AGENTS.md header; the arrow (U+2192) shows
# up in store markdown.
_EM_DASH = "—"
_ARROW = "→"


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


# --- AGENTS.md Manual section: parse must tolerate a raw non-UTF-8 byte ---


def test_parse_preserved_sections_survives_non_utf8_manual(tmp_path: Path):
    # The # Manual section is user-editable, so a legacy byte pasted in must not
    # crash the parse that sync runs on every regeneration.
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_bytes(b"# AGENTS\n\n# Manual\n\nNote: caf\xe9 shipped\n")

    preserved = parse_preserved_sections(agents_md)

    assert preserved.manual is not None
    assert "Note: caf" in preserved.manual
    assert "shipped" in preserved.manual


def test_regenerate_survives_poisoned_existing_agents_md(tmp_path: Path):
    # Full sync path: a poisoned # Manual section in an existing AGENTS.md must
    # not crash regeneration, and the manual text must survive the round-trip.
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("myproj", [repo])
    scaffold_project_store("myproj", store)

    agents_md = repo / "AGENTS.md"
    agents_md.write_bytes(b"# AGENTS.md\n\nOld auto content\n\n# Manual\n\nKeep caf\xe9 note\n")

    updated = regenerate_agents_md_for_project("myproj", store)

    assert repo in updated
    regenerated = read_text_lenient(agents_md)
    assert "Keep caf" in regenerated
    assert "note" in regenerated
    assert "Old auto content" not in regenerated


# --- Content writes emit UTF-8 regardless of locale (raw-bytes assertions) ---


def test_scaffold_writes_utf8_bytes(tmp_path: Path):
    store = tmp_path / "projects" / "myproj"
    scaffold_project_store("myproj", store)

    decision = sorted((store / constants.DECISIONS_DIR).glob("*.md"))[0]
    raw = decision.read_bytes()
    # The scaffold decision template carries the load-bearing em-dash.
    assert _EM_DASH.encode("utf-8") in raw
    assert raw == decision.read_text(encoding="utf-8").encode("utf-8")


def test_agents_md_write_emits_utf8_bytes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = register_project("myproj", [repo])
    scaffold_project_store("myproj", store)

    regenerate_agents_md_for_project("myproj", store)

    agents_md = repo / "AGENTS.md"
    raw = agents_md.read_bytes()
    # The generated AGENTS.md header carries the load-bearing em-dash.
    assert _EM_DASH.encode("utf-8") in raw
    assert raw == agents_md.read_text(encoding="utf-8").encode("utf-8")


def test_import_append_writes_utf8_bytes(tmp_path: Path):
    # New-file branch and append branch both carry non-ASCII through to disk.
    target = tmp_path / "stack.md"
    _append_to_store_file(target, f"Chose Postgres {_ARROW} Redis cache layer")
    raw_new = target.read_bytes()
    assert _ARROW.encode("utf-8") in raw_new

    _append_to_store_file(target, f"Added {_EM_DASH} background worker")
    raw_appended = target.read_bytes()
    assert _EM_DASH.encode("utf-8") in raw_appended
    assert raw_appended == target.read_text(encoding="utf-8").encode("utf-8")


def test_filesystem_store_write_emits_utf8_bytes(tmp_path: Path):
    store_path = tmp_path / "projects" / "myproj"
    store_path.mkdir(parents=True)
    store = FilesystemStore(store_path)

    content = f"# State\n\nMigrated auth {_ARROW} OAuth, dropped sessions {_EM_DASH} legacy\n"
    store.write_file(constants.STATE_CURRENT_FILENAME, content)

    target = store_path / constants.STATE_CURRENT_FILENAME
    raw = target.read_bytes()
    assert _ARROW.encode("utf-8") in raw
    assert _EM_DASH.encode("utf-8") in raw
    assert raw == content.encode("utf-8")
