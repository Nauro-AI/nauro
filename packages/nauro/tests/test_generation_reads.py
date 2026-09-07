from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest
from nauro_core import operations
from nauro_core.decision_model import Decision, DecisionConfidence, format_decision

from nauro.mcp import generation_reads as reads
from nauro.store import generation_installation as installation
from nauro.store.generation_authority import RefreshRequiredError, ReplicaActorMismatchError
from nauro.store.generation_store import GenerationSnapshotStore, GenerationStorePathError
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID, _projection
from tests.test_generation_refresh import _target

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durability implementation")
CASES = [
    ("get_context", (0,), {}),
    ("get_context", (1,), {}),
    ("get_context", (2,), {}),
    ("get_decision", (1,), {}),
    ("get_decision", (1, "header"), {}),
    ("get_raw_file", ("state.md",), {}),
    ("get_raw_file", ("project.md",), {}),
    ("list_decisions", (), {"limit": 1, "include_superseded": True}),
    ("search_decisions", ("durability",), {"limit": 1, "include_superseded": True}),
    ("check_decision", ("Use durability barriers", "interrupted refresh"), {}),
]


@pytest.fixture
def admitted(monkeypatch):
    projection = _projection(
        {
            "project.md": b"# Project\n\n## Goals\n- Keep durable evidence.\n",
            "state.md": b"# State\n\nVerified generation state.\n",
            "decisions/001-durability.md": format_decision(
                Decision(
                    num=1,
                    title="Require durability barriers",
                    date=date(2026, 9, 6),
                    confidence=DecisionConfidence.high,
                    rationale="Preserve evidence after interrupted refresh.",
                )
            ).encode(),
        }
    )
    binding = projection.target.binding
    binding.store_path.mkdir(parents=True)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    installation.publish_generation_control(installation.install_generation_root(projection))
    current = [projection]
    checks = []

    def authorize(binding, *, active_user_id, session):
        checks.append((binding, active_user_id, session))
        return current[0].target

    monkeypatch.setattr(refresh, "acquire_generation_projection", lambda *a, **k: current[0])
    monkeypatch.setattr(refresh, "check_generation_projection", authorize)
    refresh.commit_generation_refresh(
        refresh.prepare_initial_generation_refresh(binding, actor=USER_ID)
    )
    checks.clear()
    return binding, current, checks


@POSIX
@pytest.mark.parametrize("name,args,kwargs", CASES)
def test_each_read_uses_one_admitted_snapshot(admitted, name, args, kwargs):
    binding, current, checks = admitted
    projection = current[0]
    expected = getattr(operations, name)(GenerationSnapshotStore(projection), *args, **kwargs)
    (binding.store_path / "state.md").write_text("STALE FLAT STORE")
    (binding.store_path / "project.md").write_text("STALE PROJECT IDENTITY")
    for _ in range(2):
        response = getattr(reads, name)(binding, *args, actor=USER_ID, **kwargs)
        assert response.result == expected
        assert response.projection == projection.target.identity
        assert "STALE" not in repr(response)
        assert len(checks) == 4
        assert all(item == (binding, USER_ID, None) for item in checks)
        checks.clear()


@POSIX
@pytest.mark.parametrize("name,args,kwargs", CASES)
@pytest.mark.parametrize("change", ["scope", "account", "network"])
def test_authority_change_during_rendering_denies_every_result(
    admitted, monkeypatch, name, args, kwargs, change
):
    binding, current, _ = admitted
    original = getattr(operations, name)

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "scope":
            current[0] = _target()
        elif change == "account":
            monkeypatch.setattr(
                installation, "read_active_user_id", lambda: "01K44444444444444444444444"
            )
        else:

            def unavailable(*a, **k):
                raise RefreshRequiredError("Authorization unavailable.")

            monkeypatch.setattr(refresh, "check_generation_projection", unavailable)
        return result

    monkeypatch.setattr(operations, name, interrupted)
    expected = ReplicaActorMismatchError if change == "account" else RefreshRequiredError
    with pytest.raises(expected):
        getattr(reads, name)(binding, *args, actor=USER_ID, **kwargs)


@POSIX
@pytest.mark.parametrize("name,args,kwargs", CASES)
@pytest.mark.parametrize("failure", ["missing_intent", "partial", "barrier"])
def test_failed_admission_never_calls_core(admitted, monkeypatch, name, args, kwargs, failure):
    binding, _, _ = admitted
    paths = refresh.refresh_paths(binding, USER_ID)
    if failure == "missing_intent":
        paths.intent.unlink()
    elif failure == "partial":
        intent = refresh.decode_intent(paths.intent.read_bytes())
        paths.pointer.write_text(intent.base_pointer_json)
    else:

        def fail(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(refresh, "sync_file", fail)

    def forbidden(*args, **kwargs):
        pytest.fail("The core operation ran without admission.")

    monkeypatch.setattr(operations, name, forbidden)
    expected = (
        refresh.GenerationRefreshDurabilityError if failure == "barrier" else RefreshRequiredError
    )
    with pytest.raises(expected):
        getattr(reads, name)(binding, *args, actor=USER_ID, **kwargs)


@POSIX
def test_snapshot_identity_survives_local_pointer_change_during_rendering(admitted, monkeypatch):
    binding, current, _ = admitted
    original = operations.get_raw_file
    paths = refresh.refresh_paths(binding, USER_ID)
    raw = paths.pointer.read_bytes()

    def advance(store, path):
        paths.pointer.write_bytes(b"conflicting later local observation")
        return original(store, path)

    monkeypatch.setattr(operations, "get_raw_file", advance)
    result = reads.get_raw_file(binding, "state.md", actor=USER_ID)
    assert result.projection == current[0].target.identity
    assert result.result.content == "# State\n\nVerified generation state.\n"
    assert paths.pointer.read_bytes() == b"conflicting later local observation"
    paths.pointer.write_bytes(raw)


@POSIX
def test_missing_protected_file_and_invalid_path_remain_distinct(admitted):
    binding, _, _ = admitted
    missing = reads.get_raw_file(binding, "stack.md", actor=USER_ID)
    assert missing.result.model_dump(exclude_none=True) == {
        "error": {"kind": "error", "reason": "File not found: stack.md"}
    }
    with pytest.raises(GenerationStorePathError) as raised:
        reads.get_raw_file(binding, "../state.md", actor=USER_ID)
    assert raised.value.code == "generation_store_invalid_path"


def test_store_retains_detached_verified_identity():
    projection = _projection()
    store = GenerationSnapshotStore(projection)
    expected = store.target.identity.model_dump()
    object.__setattr__(projection.target.identity, "generation_id", "invalid")
    assert store.target.identity.model_dump() == expected
    with pytest.raises(FrozenInstanceError):
        store.target = projection.target


def test_adapter_has_no_production_consumer():
    root = Path(reads.__file__).parents[1]
    consumers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "nauro.mcp.generation_reads"
                or node.module == "nauro.mcp"
                and any(a.name == "generation_reads" for a in node.names)
            ):
                consumers.append(path.relative_to(root).as_posix())
            elif isinstance(node, ast.Import):
                consumers.extend(
                    a.name for a in node.names if a.name == "nauro.mcp.generation_reads"
                )
    assert sorted(set(consumers)) == ["mcp/generation_responses.py"]


@POSIX
def test_results_contain_the_installed_decision(admitted):
    binding, _, _ = admitted
    listed = reads.list_decisions(binding, actor=USER_ID).result
    assert [(row.number, row.title, row.status) for row in listed.decisions] == [
        (1, "Require durability barriers", "active")
    ]
    searched = reads.search_decisions(binding, "durability", actor=USER_ID).result
    assert [(row.number, row.title) for row in searched.results] == [
        (1, "Require durability barriers")
    ]
    checked = reads.check_decision(binding, "Require durability barriers", actor=USER_ID).result
    assert [row.id for row in checked.related_decisions] == ["decision-001"]


@POSIX
@pytest.mark.parametrize("final_status", [200, 403])
def test_http_authorization_repeats_without_artifact_downloads(tmp_path, monkeypatch, final_status):
    from dataclasses import replace

    import httpx

    from nauro.sync.generation_acquisition import acquire_generation_projection
    from nauro.sync.remote import TransferBoundaryError
    from tests.conftest import seed_auth_config
    from tests.test_sync.test_generation_acquisition import BINDING, FakeServer

    seed_auth_config(variant="sync")
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    binding = replace(BINDING, store_path=tmp_path / BINDING.project_id)
    binding.store_path.mkdir()
    server = FakeServer({"state.md": b"HTTP authorized state"})
    try:
        projection = acquire_generation_projection(
            binding, active_user_id=USER_ID, session=server.session
        )
        installation.publish_generation_control(installation.install_generation_root(projection))
        prepared = refresh.prepare_initial_generation_refresh(
            binding, actor=USER_ID, session=server.session
        )
        refresh.commit_generation_refresh(prepared, session=server.session)
        server.counts = {"projection": 0, "presign": 0, "object": 0}
        server.requests.clear()
        server.projection_hook = lambda n, p: (
            httpx.Response(final_status) if n == 4 and final_status == 403 else p
        )
        if final_status == 403:
            with pytest.raises(TransferBoundaryError) as raised:
                reads.get_raw_file(binding, "state.md", actor=USER_ID, session=server.session)
            assert raised.value.status == 403
        else:
            result = reads.get_raw_file(binding, "state.md", actor=USER_ID, session=server.session)
            assert result.result.content == "HTTP authorized state"
            assert result.projection == projection.target.identity
        assert server.counts == {"projection": 4, "presign": 0, "object": 0}
        assert [bearer for _, _, _, bearer in server.requests] == ["Bearer tok_orig"] * 4
    finally:
        server.session.client.close()


@POSIX
def test_next_read_requires_new_authorization_after_refresh(admitted):
    binding, current, checks = admitted
    before = reads.get_raw_file(binding, "state.md", actor=USER_ID)
    current[0] = _target()
    with pytest.raises(RefreshRequiredError):
        reads.get_raw_file(binding, "state.md", actor=USER_ID)
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    checks.clear()
    after = reads.get_raw_file(binding, "state.md", actor=USER_ID)
    assert before.result.content == "# State\n\nVerified generation state.\n"
    assert after.result.content == "fresh state\n"
    assert after.projection == current[0].target.identity
    assert before.projection.generation_id != after.projection.generation_id
    assert len(checks) == 4


@POSIX
def test_corrupt_installed_bytes_refuse_before_rendering(admitted, monkeypatch):
    from nauro.store.generation_projection import GenerationProjectionVerificationError

    binding, current, _ = admitted
    root = installation.install_generation_root(current[0]).root_path
    (root / "store" / "state.md").write_text("corrupt")

    def forbidden(*args, **kwargs):
        pytest.fail("Corrupt generation reached the core operation.")

    monkeypatch.setattr(operations, "get_raw_file", forbidden)
    with pytest.raises(GenerationProjectionVerificationError) as raised:
        reads.get_raw_file(binding, "state.md", actor=USER_ID)
    assert raised.value.code == "generation_verification_failed"


@POSIX
def test_project_frame_read_preserves_unpublished_flat_edits(admitted):
    binding, current, _ = admitted
    edited = b"# Project\n\nUnpublished local scope changes.\n"
    flat = binding.store_path / "project.md"
    flat.write_bytes(edited)
    expected = operations.get_raw_file(GenerationSnapshotStore(current[0]), "project.md")
    response = reads.get_raw_file(binding, "project.md", actor=USER_ID)
    assert response.result == expected
    assert response.projection == current[0].target.identity
    assert flat.read_bytes() == edited
    assert "Unpublished" not in repr(response)
