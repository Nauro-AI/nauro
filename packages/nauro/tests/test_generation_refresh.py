from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from nauro.store import generation_installation as installation
from nauro.store import generation_refresh_io as durable
from nauro.store.generation_authority import RefreshRequiredError, ReplicaActorMismatchError
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    verify_generation_projection,
)
from nauro.store.generation_read import read_installed_generation
from nauro.store.generation_refresh_intent import decode_intent, encode_intent
from nauro.store.generation_refresh_state import GenerationRefreshEvidenceError
from nauro.sync import generation_refresh as refresh
from tests.test_generation_installation import USER_ID, _projection

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durability implementation")


def _target(generation="01K66666666666666666666666", scope="b" * 64):
    seed = _projection({"state.md": b"fresh state\n"})
    manifest = json.loads(seed.manifest_json)
    manifest.update(generation_id=generation, projection_scope_id=scope)
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    fields = seed.target.identity.model_dump()
    fields.update(
        generation_id=generation,
        projection_scope_id=scope,
        manifest_digest=hashlib.sha256(raw).hexdigest(),
    )
    return verify_generation_projection(
        GenerationProjectionTarget(seed.target.binding, GenerationProjectionIdentity(**fields)),
        manifest_json=raw,
        artifacts=(("state.md", b"fresh state\n"),),
    )


@pytest.fixture
def replica(monkeypatch):
    base = _projection()
    base.target.binding.store_path.mkdir(parents=True)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    root = installation.install_generation_root(base)
    installation.publish_generation_control(root)
    current = [_target()]
    monkeypatch.setattr(refresh, "acquire_generation_projection", lambda *a, **k: current[0])
    monkeypatch.setattr(refresh, "check_generation_projection", lambda *a, **k: current[0].target)
    return base.target.binding, current


def _bootstrap(binding):
    return refresh.prepare_initial_generation_refresh(binding, actor=USER_ID)


def _active(binding):
    paths = durable.refresh_paths(binding, USER_ID)
    raw = paths.intent.read_bytes()
    return paths, raw, decode_intent(raw)


@POSIX
def test_commit_and_each_read_repeat_completion_barriers(replica, monkeypatch):
    binding, _ = replica
    result = refresh.commit_generation_refresh(_bootstrap(binding))
    assert result.read_file("state.md") == "fresh state\n"
    paths, raw, intent = _active(binding)
    assert encode_intent(intent) == raw
    calls = []
    original = refresh.sync_file

    def sync(paths, path):
        calls.append(path)
        original(paths, path)

    monkeypatch.setattr(refresh, "sync_file", sync)
    for _ in range(2):
        assert (
            refresh.admit_generation_store(binding, actor=USER_ID).read_file("state.md")
            == "fresh state\n"
        )
        assert paths.pointer in calls and paths.carrier in calls and paths.intent in calls
        calls.clear()
    with pytest.raises(RefreshRequiredError):
        read_installed_generation(
            binding,
            active_user_id=USER_ID,
            active_projection_scope_id=json.loads(intent.target_authorization_json)[
                "projection_scope_id"
            ],
        )
    with pytest.raises(RefreshRequiredError):
        installation.publish_generation_control(installation.install_generation_root(_target()))


@POSIX
@pytest.mark.parametrize("state", ["base_present", "carrier_published", "target_present"])
def test_revoked_scope_reconciles_each_partial_state_and_preserves_evidence(
    replica, monkeypatch, state
):
    binding, current = replica
    prepared = _bootstrap(binding)
    original = refresh.durable_replace

    def fail(paths, path, raw):
        if (state == "base_present" and path == paths.carrier) or (
            state == "carrier_published" and path == paths.pointer
        ):
            raise OSError("disk full")
        original(paths, path, raw)
        if state == "target_present" and path == paths.pointer:
            raise OSError("final barrier uncertain")

    monkeypatch.setattr(refresh, "durable_replace", fail)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.commit_generation_refresh(prepared)
    paths, old_raw, old_intent = _active(binding)
    assert (
        old_intent.classify(
            paths.marker.read_bytes(), paths.pointer.read_bytes(), paths.carrier.read_bytes()
        )
        == state
    )
    observed = paths.pointer.read_bytes(), paths.carrier.read_bytes()
    current[0] = _target("01K77777777777777777777777", "c" * 64)
    with pytest.raises(RefreshRequiredError):
        refresh.admit_generation_store(binding, actor=USER_ID)
    monkeypatch.setattr(refresh, "durable_replace", original)
    assert (
        refresh.recover_generation_refresh(binding, actor=USER_ID).read_file("state.md")
        == "fresh state\n"
    )
    _, new_raw, successor = _active(binding)
    assert successor.kind == "reconcile"
    assert successor.predecessor_digest == hashlib.sha256(old_raw).hexdigest()
    assert (paths.history / f"{successor.predecessor_digest}.json").read_bytes() == old_raw
    assert (
        successor.base_pointer_json.encode(),
        successor.base_authorization_json.encode(),
    ) == observed
    assert new_raw != old_raw


@POSIX
def test_repeated_reconciliation_preserves_each_predecessor(replica, monkeypatch):
    binding, current = replica
    original = refresh.durable_replace

    def fail(paths, path, raw):
        original(paths, path, raw)
        if path == paths.carrier:
            raise OSError("lost process")

    monkeypatch.setattr(refresh, "durable_replace", fail)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.commit_generation_refresh(_bootstrap(binding))
    first = _active(binding)[1]
    current[0] = _target("01K77777777777777777777777", "c" * 64)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.recover_generation_refresh(binding, actor=USER_ID)
    second = _active(binding)[1]
    current[0] = _target("01K88888888888888888888888", "d" * 64)
    monkeypatch.setattr(refresh, "durable_replace", original)
    refresh.recover_generation_refresh(binding, actor=USER_ID)
    paths, _, _ = _active(binding)
    for raw in (first, second):
        assert (paths.history / f"{hashlib.sha256(raw).hexdigest()}.json").read_bytes() == raw


@POSIX
@pytest.mark.parametrize("file", ["marker", "intent", "pointer", "carrier"])
def test_target_present_is_not_admitted_after_barrier_failure(replica, monkeypatch, file):
    binding, _ = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    paths, before, _ = _active(binding)
    original = refresh.sync_file

    def fail(paths, path):
        if path == getattr(paths, file):
            raise OSError("barrier failed")
        original(paths, path)

    monkeypatch.setattr(refresh, "sync_file", fail)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.admit_generation_store(binding, actor=USER_ID)
    assert paths.intent.read_bytes() == before
    monkeypatch.setattr(refresh, "sync_file", original)
    assert (
        refresh.admit_generation_store(binding, actor=USER_ID).read_file("state.md")
        == "fresh state\n"
    )


@POSIX
def test_authorization_failure_and_changed_account_deny_without_mutation(replica, monkeypatch):
    binding, _ = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    paths, before, _ = _active(binding)

    def revoked(*args, **kwargs):
        raise RefreshRequiredError("revoked")

    monkeypatch.setattr(refresh, "check_generation_projection", revoked)
    with pytest.raises(RefreshRequiredError):
        refresh.admit_generation_store(binding, actor=USER_ID)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: "01K88888888888888888888888")
    with pytest.raises(ReplicaActorMismatchError):
        refresh.recover_generation_refresh(binding, actor=USER_ID)
    assert paths.intent.read_bytes() == before


@POSIX
def test_stale_preparation_and_missing_intent_refuse(replica):
    binding, _ = replica
    with pytest.raises(RefreshRequiredError):
        refresh.prepare_generation_refresh(binding, actor=USER_ID)
    one, two = _bootstrap(binding), _bootstrap(binding)
    refresh.commit_generation_refresh(one)
    with pytest.raises(GenerationRefreshEvidenceError):
        refresh.commit_generation_refresh(two)


@POSIX
@pytest.mark.parametrize("boundary", ["intent", "carrier", "pointer"])
def test_process_loss_after_replacement_recovers_from_exact_evidence(replica, boundary):
    binding, _ = replica
    script = """
import os
from nauro.store import generation_installation as installation
from nauro.sync import generation_refresh as refresh
from tests.test_generation_refresh import _target
from tests.test_generation_installation import USER_ID
installation.read_active_user_id = lambda: USER_ID
projection = _target()
refresh.acquire_generation_projection = lambda *a, **k: projection
refresh.check_generation_projection = lambda *a, **k: projection.target
original = refresh.durable_replace
def stop(paths, path, raw):
    original(paths, path, raw)
    if path == getattr(paths, os.environ["REFRESH_STOP"]):
        os._exit(73)
refresh.durable_replace = stop
prepared = refresh.prepare_initial_generation_refresh(projection.target.binding, actor=USER_ID)
refresh.commit_generation_refresh(prepared)
"""
    env = dict(os.environ, REFRESH_STOP=boundary)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).parent.parent)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 73, result.stderr.decode()
    old = _active(binding)[1]
    assert (
        refresh.recover_generation_refresh(binding, actor=USER_ID).read_file("state.md")
        == "fresh state\n"
    )
    assert _active(binding)[1] == old


@POSIX
@pytest.mark.parametrize("failure", ["archive_barrier", "archive_corrupt", "archive_missing"])
def test_predecessor_failures_refuse_without_replacing_active_intent(replica, monkeypatch, failure):
    binding, current = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    paths, before, _ = _active(binding)
    current[0] = _target("01K77777777777777777777777", "c" * 64)
    prepared = refresh.prepare_generation_refresh(binding, actor=USER_ID)
    archive = paths.history / f"{hashlib.sha256(before).hexdigest()}.json"
    if failure == "archive_barrier":
        original = durable.sync_file

        def fail(paths, path):
            if path == archive:
                raise OSError("archive barrier")
            original(paths, path)

        monkeypatch.setattr(durable, "sync_file", fail)
        expected = refresh.GenerationRefreshDurabilityError
    elif failure == "archive_corrupt":
        paths.history.mkdir()
        archive.write_bytes(b"corrupt")
        expected = GenerationRefreshEvidenceError
    else:
        refresh.commit_generation_refresh(prepared)
        archive.unlink()
        with pytest.raises(GenerationRefreshEvidenceError):
            refresh.admit_generation_store(binding, actor=USER_ID)
        return
    with pytest.raises(expected):
        refresh.commit_generation_refresh(prepared)
    assert paths.intent.read_bytes() == before


@POSIX
def test_final_authorization_change_prevents_disclosure(replica, monkeypatch):
    binding, current = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    calls = []

    def changing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            return _target("01K77777777777777777777777", "c" * 64).target
        return current[0].target

    monkeypatch.setattr(refresh, "check_generation_projection", changing)
    with pytest.raises(RefreshRequiredError):
        refresh.admit_generation_store(binding, actor=USER_ID)
    assert len(calls) == 3


@POSIX
@pytest.mark.parametrize("boundary", ["manifest", "artifact", "directory"])
def test_target_data_and_directory_barrier_failures_deny_admission(replica, monkeypatch, boundary):
    binding, _ = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    original_file = refresh.sync_file
    original_parents = refresh.sync_parents

    def file_sync(paths, path):
        if (boundary == "manifest" and path.name == "manifest.json") or (
            boundary == "artifact" and path.name == "state.md"
        ):
            raise OSError("data barrier failed")
        original_file(paths, path)

    def directories(paths, path):
        if boundary == "directory":
            raise OSError("directory barrier failed")
        original_parents(paths, path)

    monkeypatch.setattr(refresh, "sync_file", file_sync)
    monkeypatch.setattr(refresh, "sync_parents", directories)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.admit_generation_store(binding, actor=USER_ID)


def test_unsupported_directory_barrier_stops_before_intent_or_control_mutation(
    replica, monkeypatch
):
    binding, _ = replica
    paths = durable.refresh_paths(binding, USER_ID)
    before = paths.pointer.read_bytes(), paths.carrier.read_bytes()

    def unsupported(*args):
        raise OSError("directory durability unavailable")

    monkeypatch.setattr(refresh, "sync_parents", unsupported)
    with pytest.raises(refresh.GenerationRefreshDurabilityError):
        refresh.commit_generation_refresh(_bootstrap(binding))
    assert (paths.pointer.read_bytes(), paths.carrier.read_bytes()) == before
    assert not paths.intent.exists()


@POSIX
@pytest.mark.parametrize("boundary", ["archive", "archive_link", "intent", "carrier", "pointer"])
def test_process_loss_during_reconciliation_keeps_predecessor_and_resumes(replica, boundary):
    binding, current = replica
    refresh.commit_generation_refresh(_bootstrap(binding))
    predecessor = _active(binding)[1]
    script = """
import os
from nauro.store import generation_installation as installation
from nauro.sync import generation_refresh as refresh
from tests.test_generation_refresh import _target
from tests.test_generation_installation import USER_ID
installation.read_active_user_id = lambda: USER_ID
projection = _target('01K77777777777777777777777', 'c' * 64)
refresh.acquire_generation_projection = lambda *a, **k: projection
refresh.check_generation_projection = lambda *a, **k: projection.target
original = refresh.durable_replace
archive = refresh.preserve_predecessor
link = os.link
def linked(source, destination):
    link(source, destination)
    if os.environ['REFRESH_STOP'] == 'archive_link':
        os._exit(73)
os.link = linked
def save(paths, digest, raw):
    archive(paths, digest, raw)
    if os.environ['REFRESH_STOP'] == 'archive':
        os._exit(73)
def stop(paths, path, raw):
    original(paths, path, raw)
    boundary = os.environ['REFRESH_STOP']
    if boundary not in ('archive', 'archive_link') and path == getattr(paths, boundary):
        os._exit(73)
refresh.durable_replace = stop
refresh.preserve_predecessor = save
refresh.recover_generation_refresh(projection.target.binding, actor=USER_ID)
"""
    env = dict(os.environ, REFRESH_STOP=boundary)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).parent.parent)
    process = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=30
    )
    assert process.returncode == 73, process.stderr.decode()
    current[0] = _target("01K77777777777777777777777", "c" * 64)
    assert (
        refresh.recover_generation_refresh(binding, actor=USER_ID).read_file("state.md")
        == "fresh state\n"
    )
    paths, _, _ = _active(binding)
    assert (
        paths.history / f"{hashlib.sha256(predecessor).hexdigest()}.json"
    ).read_bytes() == predecessor


def test_intent_codec_round_trip_and_integrity(replica):
    binding, _ = replica
    intent = refresh._new_intent(_bootstrap(binding))
    raw = encode_intent(intent)
    assert decode_intent(raw) == intent
    assert encode_intent(decode_intent(raw)) == raw
    envelope = json.loads(raw)
    envelope["digest"] = "0" * 64
    with pytest.raises(GenerationRefreshEvidenceError):
        decode_intent(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(GenerationRefreshEvidenceError):
        decode_intent(raw + b"\n")
    with pytest.raises(GenerationRefreshEvidenceError):
        decode_intent(b'{"payload":{},"payload":{},"digest":"x"}')


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("predecessor_digest", "invalid"),
        ("kind", "rollback"),
    ],
)
def test_intent_rejects_invalid_schema(replica, field, value):
    from pydantic import ValidationError

    from nauro.store.generation_refresh_intent import RefreshIntent

    binding, _ = replica
    facts = refresh._new_intent(_bootstrap(binding)).model_dump()
    facts[field] = value
    with pytest.raises(ValidationError):
        RefreshIntent.model_validate(facts)


@POSIX
@pytest.mark.parametrize("boundary", ["intent", "carrier", "pointer"])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_process_exit_around_replacement_directory_barrier(replica, boundary, phase):
    binding, _ = replica
    script = """
import os
from nauro.store import generation_installation as installation
from nauro.store import generation_refresh_io as durable
from nauro.sync import generation_refresh as refresh
from tests.test_generation_refresh import _target
from tests.test_generation_installation import USER_ID
installation.read_active_user_id = lambda: USER_ID
projection = _target()
refresh.acquire_generation_projection = lambda *a, **k: projection
refresh.check_generation_projection = lambda *a, **k: projection.target
paths = durable.refresh_paths(projection.target.binding, USER_ID)
replace = durable.os.replace
barrier = durable.sync_directory
armed = False
def install(source, destination):
    global armed
    replace(source, destination)
    if destination == getattr(paths, os.environ['REFRESH_STOP']):
        armed = True
def sync(paths, directory):
    if armed and os.environ['REFRESH_PHASE'] == 'before':
        os._exit(73)
    barrier(paths, directory)
    if armed and os.environ['REFRESH_PHASE'] == 'after':
        os._exit(73)
durable.os.replace = install
durable.sync_directory = sync
prepared = refresh.prepare_initial_generation_refresh(projection.target.binding, actor=USER_ID)
refresh.commit_generation_refresh(prepared)
"""
    env = dict(os.environ, REFRESH_STOP=boundary, REFRESH_PHASE=phase)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).parent.parent)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 73, result.stderr.decode()
    assert (
        refresh.recover_generation_refresh(binding, actor=USER_ID).read_file("state.md")
        == "fresh state\n"
    )


def test_refresh_executor_has_only_named_runtime_consumers():
    import ast
    from pathlib import Path

    root = Path(refresh.__file__).parents[1]
    consumers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "nauro.sync.generation_refresh":
                consumers.append(path.relative_to(root).as_posix())
            elif isinstance(node, ast.Import):
                consumers.extend(
                    a.name for a in node.names if a.name == "nauro.sync.generation_refresh"
                )
    assert sorted(consumers) == [
        "cli/generation_reads.py",
        "mcp/generation_reads.py",
        "mcp/generation_responses.py",
        "sync/generation_attachment.py",
    ]
