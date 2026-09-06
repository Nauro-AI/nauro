from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from nauro_core.operations.get_decision import get_decision
from nauro_core.operations.store import Store

from nauro.store import generation_installation as installation
from nauro.store import generation_store
from nauro.store.generation_projection import GenerationProjectionVerificationError
from nauro.store.generation_store import (
    GenerationSnapshotStore,
    GenerationStorePathError,
    GenerationStoreReadOnlyError,
    capture_generation_store,
)
from tests.test_generation_installation import SCOPE_ID, USER_ID, _projection


@pytest.fixture
def projection():
    return _projection(
        {
            "decisions/002-second.md": b"# Second\n",
            "decisions/001-first.md": b"# First\n",
            "context/brief.md": b"Context\n",
            "state.md": b"State\n",
        }
    )


def test_store_implements_reads_and_returns_fresh_lists(projection):
    store: Store = GenerationSnapshotStore(projection)
    assert isinstance(store, Store)
    assert store.list_decisions() == ["001-first", "002-second"]
    stems = store.list_decisions()
    stems.clear()
    assert store.list_decisions() == ["001-first", "002-second"]
    assert store.read_file("state.md") == "State\n"
    assert store.read_file("project.md") is None
    assert store.read_decision("001-first") == "# First\n"
    assert store.read_decisions(["002-second", "999-missing", "001-first"]) == {
        "002-second": "# Second\n",
        "999-missing": None,
        "001-first": "# First\n",
    }
    assert get_decision(store, 1).content == "# First\n"


@pytest.mark.parametrize(
    "path",
    [
        "../state.md",
        "/state.md",
        "./state.md",
        "context//brief.md",
        "context\\brief.md",
        ".replica/authority.json",
        "unknown.md",
        "decisions/sub/file.md",
        "",
    ],
)
def test_store_refuses_noncanonical_and_nonprotected_paths(projection, path):
    with pytest.raises(GenerationStorePathError) as raised:
        GenerationSnapshotStore(projection).read_file(path)
    assert raised.value.code == "generation_store_invalid_path"


@pytest.mark.parametrize("stem", ["../state", "/state", "nested/decision", ""])
def test_decision_reads_cannot_escape(projection, stem):
    store = GenerationSnapshotStore(projection)
    with pytest.raises(GenerationStorePathError):
        store.read_decision(stem)
    with pytest.raises(GenerationStorePathError):
        store.read_decisions(["001-first", stem])


@pytest.mark.parametrize("path", ["state.md", "project.md", "../outside.md"])
def test_every_mutation_refuses(projection, path):
    store = GenerationSnapshotStore(projection)
    with pytest.raises(GenerationStoreReadOnlyError) as write:
        store.write_file(path, "changed")
    with pytest.raises(GenerationStoreReadOnlyError) as delete:
        store.delete_file(path)
    assert write.value.code == delete.value.code == "generation_store_read_only"
    assert store.read_file("state.md") == "State\n"


def test_store_uses_canonical_bytes_and_detaches_metadata(projection):
    object.__setattr__(projection.manifest, "artifacts", {})
    store = GenerationSnapshotStore(projection)
    assert store.read_decision("001-first") == "# First\n"
    object.__setattr__(projection, "artifacts", ())
    assert store.read_decision("001-first") == "# First\n"
    with pytest.raises(FrozenInstanceError):
        store._contents = {}
    with pytest.raises(TypeError):
        store._contents["state.md"] = "changed"


def test_store_reverifies_artifact_bytes(projection):
    object.__setattr__(projection.artifacts[0], "content", b"corrupt")
    with pytest.raises(GenerationProjectionVerificationError):
        GenerationSnapshotStore(projection)


def test_store_matches_replacement_decoding():
    store = GenerationSnapshotStore(_projection({"state.md": b"invalid \xff\n"}))
    assert store.read_file("state.md") == "invalid \ufffd\n"


def test_capture_composition_survives_removed_disk_root(projection, monkeypatch):
    import shutil

    binding = projection.target.binding
    binding.store_path.mkdir(parents=True)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    installed = installation.install_generation_root(projection)
    installation.publish_generation_control(installed)
    store = capture_generation_store(
        binding, active_user_id=USER_ID, active_projection_scope_id=SCOPE_ID, timeout=0
    )
    shutil.rmtree(installed.root_path)
    assert get_decision(store, 2).content == "# Second\n"
    assert store.read_file("context/brief.md") == "Context\n"


def test_store_has_only_the_dormant_refresh_consumer():
    root = Path(generation_store.__file__).parents[1]
    consumers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "nauro.store.generation_store":
                consumers.append(path.relative_to(root).as_posix())
            if isinstance(node, ast.Import):
                consumers.extend(
                    alias.name
                    for alias in node.names
                    if alias.name == "nauro.store.generation_store"
                )
    assert consumers == ["sync/generation_refresh.py"]
