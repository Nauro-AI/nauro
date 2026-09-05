from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from inspect import signature
from pathlib import Path

import pytest
from filelock import FileLock

from nauro.auth import ActiveUserReadError
from nauro.mcp.tools import tool_check_decision, tool_get_raw_file
from nauro.store import generation_installation as installation
from nauro.store import replica_control
from nauro.store.generation_authority import (
    FlatProjectAuthority,
    GenerationAuthorityError,
    GenerationControlCorruptError,
    GenerationProjectAuthority,
    InstalledAuthorizationView,
    InstalledGenerationPointer,
    RefreshRequiredError,
    ReplicaActorMismatchError,
)
from nauro.store.generation_installation import (
    GenerationControlPublicationError,
    GenerationInstallError,
    GenerationRootAudit,
    GenerationRootDivergedError,
    InstalledGenerationRoot,
    StagedGenerationRoot,
    audit_generation_tree,
    install_generation_root,
    publish_generation_control,
    stage_generation_root,
)
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    GenerationProjectionTarget,
    VerifiedGenerationProjection,
    verify_generation_projection,
)
from nauro.store.registry import get_store_path_v2
from nauro.store.replica_control import (
    ReplicaControlBusyError,
    ReplicaControlReadError,
    _is_link_or_reparse,
    locked_replica_control_snapshot,
)
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync import merge
from nauro.sync.corpus import DecisionCorpus
from nauro.sync.pull import PullReport
from nauro.sync.quarantine import list_conflict_backup_files, list_quarantine_backups
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_sync.conftest import (
    _scaffolded_cloud_project,
    _seed_token,
    entry_names,
    pull_report,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename semantics")
LINK_KINDS = ["symlink", "junction"]
PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K33333333333333333333333"
SCOPE_ID = "a" * 64
COMMITTED_AT = "2999-12-31T23:59:59.999999Z"
OTHER_COMMITTED_AT = "2998-12-31T23:59:59.999999Z"
INSTALLED_AT = "2026-09-04T01:02:03.000004Z"
FIXED_DATETIME = datetime.fromisoformat(INSTALLED_AT.replace("Z", "+00:00"))
FIXED_CLOCK = type("Clock", (), {"now": classmethod(lambda *_: FIXED_DATETIME)})
INSTALLED_STATE_ID = "01K22222222222222222222222"
OTHER_STATE_ID = "01K44444444444444444444444"
ROOT_KEY = "868847269e6f3c829a569ad5d6655bd5"
CODE = "generation_install_failed"
ACTOR = f".replica/v1/actors/{USER_ID}"
COMPONENTS = {
    ".replica": ".replica",
    "v1": ".replica/v1",
    "actors": ".replica/v1/actors",
    "actor": ACTOR,
    "generations": f"{ACTOR}/generations",
    "staging": f"{ACTOR}/staging",
}
ARTIFACTS = {
    "decisions/001-x.md": b"# 001\n",
    "context/brief.md": b"# Brief\n",
    "questions-provenance.json": b"{}",
}
FORBIDDEN_IMPORTS = set(
    (
        "httpx nauro.sync.remote nauro.sync.pull nauro.sync.state nauro.sync.hooks nauro.sync.merge"
    ).split()
)
SINGLE_CALLER_NAMES = (
    "_walk_store_files _prepare_store_root _admit_native_path _classify_sync_path".split()
)
NOT_DIRECTORY = "The generation root component is not a directory."
DIVERGED = "generation_root_diverged"
LOCK_NAME = ".replica-control.lock"
OCCUPIED = "The generation root is occupied by a non-directory entry."
PUBLISH_FAILED = "The generation root could not be published."


def _link(link: Path, target: Path, kind: str) -> None:
    if kind == "junction":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
        return
    link.symlink_to(target, target_is_directory=True)


def _require_link_kind(kind: str) -> None:
    if (kind == "junction") != (sys.platform == "win32"):
        pytest.skip(f"{kind} is not this platform's directory link")


@pytest.fixture
def store() -> Path:
    path = get_store_path_v2(PROJECT_ID)
    scaffold_project_store("nauro", path)
    return path


def _actor(store: Path) -> Path:
    return store / ".replica" / "v1" / "actors" / USER_ID


def _manifest(
    artifacts: dict[str, bytes],
    scope_id: str = SCOPE_ID,
    committed_at: str = COMMITTED_AT,
) -> bytes:
    body = {
        "project_id": PROJECT_ID,
        "store_format_version": 1,
        "generation_id": GENERATION_ID,
        "committed_at": committed_at,
        "projection_class": "contributor_plus",
        "projection_scope_id": scope_id,
        "artifacts": {path: hashlib.sha256(body).hexdigest() for path, body in artifacts.items()},
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _projection(
    artifacts: dict[str, bytes] | None = None, *, committed_at: str = COMMITTED_AT
) -> VerifiedGenerationProjection:
    artifacts = ARTIFACTS if artifacts is None else artifacts
    manifest = _manifest(artifacts, committed_at=committed_at)
    binding = ResolvedProjectBinding(
        get_store_path_v2(PROJECT_ID), PROJECT_ID, "Nauro", "cloud", "https://mcp.nauro.ai"
    )
    identity = GenerationProjectionIdentity(
        project_id=PROJECT_ID,
        store_format_version=1,
        generation_id=GENERATION_ID,
        manifest_digest=hashlib.sha256(manifest).hexdigest(),
        committed_at=committed_at,
        installed_for_user_id=USER_ID,
        projection_class="contributor_plus",
        projection_scope_id=SCOPE_ID,
    )
    target = GenerationProjectionTarget(binding, identity)
    return verify_generation_projection(
        target, manifest_json=manifest, artifacts=tuple(artifacts.items())
    )


def _tree(root: Path) -> list[str]:
    found, frames = [], [root]
    while frames:
        with os.scandir(frames.pop()) as entries:
            for entry in entries:
                found.append(Path(entry.path).relative_to(root).as_posix())
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(metadata):
                    frames.append(Path(entry.path))
    return sorted(found)


def _fails(message: str, call) -> GenerationInstallError:
    with pytest.raises(GenerationInstallError) as raised:
        call()
    assert (type(raised.value), raised.value.code) == (GenerationInstallError, CODE)
    assert str(raised.value) == message
    return raised.value


def _diverges(message: str, call) -> None:
    with pytest.raises(GenerationRootDivergedError) as raised:
        call()
    assert (type(raised.value), raised.value.code) == (GenerationRootDivergedError, DIVERGED)
    assert str(raised.value) == message


def _generations(store: Path) -> Path:
    return _actor(store) / "generations"


def _staging(store: Path) -> Path:
    return _actor(store) / "staging"


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        relative: (root / relative).read_bytes()
        for relative in _tree(root)
        if stat.S_ISREG(os.lstat(root / relative).st_mode)
    }


def _control_files(store: Path) -> tuple[Path, Path]:
    return store / ".replica" / "authority.json", _actor(store) / "pointer.json"


def _authorization_view(store: Path) -> Path:
    return _actor(store) / "authorization-view.json"


def _guard_occupant(kind: str) -> None:
    if kind in LINK_KINDS:
        _require_link_kind(kind)
    if sys.platform == "win32" and kind in ("dangling", "fifo", "unreadable"):
        pytest.skip(f"{kind} is a POSIX proof")


def _plant_occupant(path: Path, kind: str, outside: Path) -> None:
    if kind == "missing":
        return
    if kind in LINK_KINDS:
        _link(path, outside, kind)
    elif kind == "dangling":
        path.symlink_to(outside / "gone", target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside / "victim", path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"x" * (16 * 1024 + 1) if kind == "oversized" else b"occupant")
        if kind == "unreadable":
            path.chmod(0)


def _fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    if not os.path.lexists(path):
        return None
    value = os.lstat(path)
    return value.st_mode, value.st_ino, value.st_nlink, value.st_size


def _pointer_bytes(projection: VerifiedGenerationProjection, **changes: object) -> bytes:
    identity = projection.target.identity
    body: dict[str, object] = {
        "schema_version": 1,
        **{field: getattr(identity, field) for field in installation._TARGET_POINTER_FIELDS},
        "installed_at": INSTALLED_AT,
        "installed_state_id": OTHER_STATE_ID,
    }
    body.update(changes)
    return InstalledGenerationPointer(**body).canonical_bytes()


def _authorization_bytes(projection: VerifiedGenerationProjection, **changes: object) -> bytes:
    identity = projection.target.identity
    body: dict[str, object] = {
        "schema_version": 1,
        **{field: getattr(identity, field) for field in installation._TARGET_POINTER_FIELDS},
        "installed_state_id": OTHER_STATE_ID,
    }
    body.update(changes)
    return InstalledAuthorizationView(**body).canonical_bytes()


@pytest.fixture(autouse=True)
def _active_publication_actor(monkeypatch) -> None:
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID, raising=False)


class _FailDateTime:
    @classmethod
    def now(cls, _tz=None):
        pytest.fail("publication acquired a new clock value")


def _forbid_publication_effects(monkeypatch) -> None:
    monkeypatch.setattr(installation, "atomic_write_bytes", lambda *_a: pytest.fail("wrote"))
    monkeypatch.setattr(installation, "datetime", _FailDateTime)
    monkeypatch.setattr(installation, "generate_ulid", lambda: pytest.fail("minted"))


def _forbid_pointer_access(monkeypatch, pointer: Path) -> None:
    real_read = installation._read_control_file
    real_validate = installation._validate_managed_path
    real_lstat = os.lstat

    def reject(candidate, call, *args):
        if Path(candidate) == pointer:
            pytest.fail("pointer accessed")
        return call(candidate, *args)

    monkeypatch.setattr(
        installation,
        "_read_control_file",
        lambda store_path, path: reject(path, lambda value: real_read(store_path, value)),
    )
    monkeypatch.setattr(
        installation,
        "_validate_managed_path",
        lambda store_path, path: reject(path, lambda value: real_validate(store_path, value)),
    )
    monkeypatch.setattr(installation.os, "lstat", lambda path: reject(path, real_lstat))
    monkeypatch.setattr(installation, "_parse_pointer", lambda *_: pytest.fail("pointer parsed"))


def _age(path: Path, seconds: float) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def test_layout_spellings_and_flat_authority(store: Path) -> None:
    before = entry_names(store)
    projection = _projection()
    staged = stage_generation_root(projection)
    actor = _actor(store)
    assert staged.root_key == ROOT_KEY
    assert staged.root_path == actor / "generations" / ROOT_KEY
    assert staged.staging_path.parent == actor / "staging"
    prefix, _, token = staged.staging_path.name.partition("-")
    assert (prefix, len(token), set(token) <= set("0123456789abcdef")) == (ROOT_KEY, 16, True)
    assert entry_names(store) == before | {".replica"}
    assert entry_names(store / ".replica") == {"v1"}
    assert entry_names(store / ".replica" / "v1") == {"actors"}
    assert entry_names(store / ".replica" / "v1" / "actors") == {USER_ID}
    assert entry_names(actor) == {"generations", "staging"}
    assert entry_names(actor / "generations") == set()
    assert entry_names(actor / "staging") == {staged.staging_path.name}
    assert staged.target == projection.target
    with locked_replica_control_snapshot(
        projection.target.binding, active_user_id=USER_ID, active_projection_scope_id=SCOPE_ID
    ) as snapshot:
        assert snapshot.authority == FlatProjectAuthority(projection.target.binding)
    _fails(
        "Generation installation requires a verified projection.",
        lambda: stage_generation_root(object()),
    )


def test_staging_name_retries_on_collision(store: Path, monkeypatch) -> None:
    real, tokens = installation.secrets.token_hex, iter(["0" * 16, "1" * 16])
    monkeypatch.setattr(installation.secrets, "token_hex", lambda n: next(tokens, None) or real(n))
    planted = _actor(store) / "staging" / f"{ROOT_KEY}-{'0' * 16}"
    planted.mkdir(parents=True)
    staged = stage_generation_root(_projection())
    assert staged.staging_path.name == f"{ROOT_KEY}-{'1' * 16}"
    assert entry_names(planted.parent) == {planted.name, staged.staging_path.name}


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize("name", list(COMPONENTS))
def test_linked_component_is_refused_before_creation(store, tmp_path, kind, name) -> None:
    _require_link_kind(kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    component = store / COMPONENTS[name]
    component.parent.mkdir(parents=True, exist_ok=True)
    if name == "staging":
        (component.parent / "generations").mkdir()
    _link(component, outside, kind)
    before = (_tree(outside), _tree(store))
    with pytest.raises(ReplicaControlReadError) as raised:
        stage_generation_root(_projection())
    assert raised.value.code == "generation_control_unavailable"
    assert (_tree(outside), _tree(store)) == before


@pytest.mark.parametrize("name", [".replica", "actors"])
def test_regular_file_component_is_refused(store: Path, name: str) -> None:
    component = store / COMPONENTS[name]
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_bytes(b"file\n")
    before = _tree(store)
    _fails(NOT_DIRECTORY, lambda: stage_generation_root(_projection()))
    assert _tree(store) == before


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_staged_link_is_refused_before_any_write(store, tmp_path, kind) -> None:
    _require_link_kind(kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    hand = _actor(store) / "staging" / f"{ROOT_KEY}-{'0' * 16}"
    (hand / "store").mkdir(parents=True)
    _link(hand / "store" / "decisions", outside, kind)
    before = _tree(outside)
    with pytest.raises(ReplicaControlReadError):
        installation._ensure_directory(store, hand / "store" / "decisions")
    assert _tree(outside) == before


@pytest.mark.parametrize("artifacts", [ARTIFACTS, {}], ids=["three", "empty"])
def test_staged_tree_matches_projection_exactly(store: Path, artifacts) -> None:
    projection = _projection(artifacts)
    staged = stage_generation_root(projection)
    expected = {f"store/{path}": content for path, content in artifacts.items()}
    expected["manifest.json"] = projection.manifest_json
    directories = {"store"} | {
        f"store/{path.rpartition('/')[0]}" for path in artifacts if "/" in path
    }
    assert _tree(staged.staging_path) == sorted(expected.keys() | directories)
    assert {
        relative: (staged.staging_path / relative).read_bytes() for relative in expected
    } == expected
    assert staged.audit == GenerationRootAudit(
        projection.target.identity.manifest_digest,
        len(artifacts),
        sum(len(content) for content in expected.values()),
    )


def _row(mutation: str, message: str, *marks):
    return pytest.param(mutation, message, id=mutation, marks=marks)


AUDIT_ROWS = [
    _row("extra-file", "The generation root holds an unexpected entry: store/extra.md."),
    _row("extra-dir", "The generation root holds an unexpected entry: store/extra."),
    _row("missing", "The generation root is missing an entry: store/context/brief.md."),
    _row("byte", "The generation artifact diverges: decisions/001-x.md."),
    _row("symlink", "The generation root holds a link: store/context/brief.md.", POSIX_ONLY),
    _row("junction", "The generation root holds a link: store/context."),
    _row(
        "hard-link",
        "The generation root holds a linked file: store/decisions/001-x.md.",
        POSIX_ONLY,
    ),
    _row("fifo", "The generation root holds an irregular entry: manifest.json.", POSIX_ONLY),
    _row(
        "tmp", "The generation root holds a partial write: store/.project.md.0123456789abcdef.tmp."
    ),
    _row("manifest-byte", "The generation root manifest diverges."),
    _row("manifest-scope", "The generation root manifest diverges."),
]


def _mutate(tree: Path, mutation: str, projection, tmp_path: Path, monkeypatch) -> None:
    if mutation == "extra-file":
        (tree / "store" / "extra.md").write_bytes(b"extra\n")
    elif mutation == "extra-dir":
        (tree / "store" / "extra").mkdir()
    elif mutation == "missing":
        (tree / "store" / "context" / "brief.md").unlink()
    elif mutation == "byte":
        (tree / "store" / "decisions" / "001-x.md").write_bytes(b"# 002\n")
    elif mutation == "symlink":
        victim = tree / "store" / "context" / "brief.md"
        victim.unlink()
        victim.symlink_to(tree / "store" / "decisions" / "001-x.md")
    elif mutation == "junction":
        shutil.rmtree(tree / "store" / "context")
        _link(tree / "store" / "context", tree / "store" / "decisions", "junction")
    elif mutation == "hard-link":
        os.link(tree / "store" / "decisions" / "001-x.md", tmp_path / "alias.md")
    elif mutation == "fifo":
        (tree / "manifest.json").unlink()
        os.mkfifo(tree / "manifest.json")
        real_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == tree / "manifest.json":
                pytest.fail("opened the fifo")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(installation.os, "open", guarded_open)
    elif mutation == "tmp":
        (tree / "store" / ".project.md.0123456789abcdef.tmp").write_bytes(b"")
    elif mutation == "manifest-byte":
        raw = bytearray(projection.manifest_json)
        raw[-1] ^= 1
        (tree / "manifest.json").write_bytes(bytes(raw))
    else:
        (tree / "manifest.json").write_bytes(_manifest(ARTIFACTS, "b" * 64))


@pytest.mark.parametrize(("mutation", "message"), AUDIT_ROWS)
def test_audit_refuses_each_mutation(store, tmp_path, monkeypatch, mutation: str, message: str):
    if mutation in LINK_KINDS:
        _require_link_kind(mutation)
    projection = _projection()
    staging = stage_generation_root(projection).staging_path
    _mutate(staging, mutation, projection, tmp_path, monkeypatch)
    _fails(message, lambda: audit_generation_tree(staging, projection))


def test_access_order_probes_before_every_creation_and_write(store: Path, monkeypatch) -> None:
    events: list[tuple[str, Path]] = []
    real_lstat, real_mkdir, real_path_mkdir = os.lstat, os.mkdir, Path.mkdir
    real_write, real_audit = installation.atomic_write_bytes, installation.audit_generation_tree

    def lstat(path, *args, **kwargs):
        events.append(("lstat", Path(path)))
        return real_lstat(path, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        events.append(("mkdir", Path(path)))

    def path_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if parents:
            try:
                real_lstat(self)
            except FileNotFoundError:
                pytest.fail(f"mkdir(parents=True) over absent {self}")
        return real_path_mkdir(self, mode, parents, exist_ok)

    def write(path, content):
        events.append(("write", Path(path)))
        real_write(path, content)

    def audit(directory, projection):
        events.append(("audit", directory))
        return real_audit(directory, projection)

    monkeypatch.setattr(installation.os, "lstat", lstat)
    monkeypatch.setattr(installation.os, "mkdir", mkdir)
    monkeypatch.setattr(Path, "mkdir", path_mkdir)
    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "audit_generation_tree", audit)
    staging = stage_generation_root(_projection()).staging_path
    actor, control = _actor(store), store / ".replica"
    assert [path for kind, path in events if kind == "mkdir"] == [
        control,
        control / "v1",
        control / "v1" / "actors",
        actor,
        actor / "generations",
        actor / "staging",
        staging,
        staging / "store",
        staging / "store" / "context",
        staging / "store" / "decisions",
    ]
    assert [path for kind, path in events if kind == "write"] == [
        staging / "store" / "context" / "brief.md",
        staging / "store" / "decisions" / "001-x.md",
        staging / "store" / "questions-provenance.json",
        staging / "manifest.json",
    ]
    assert events.index(("lstat", control)) < events.index(("mkdir", control))
    for index, (kind, path) in enumerate(events):
        if kind == "mkdir":
            later = events[index + 1 :]
            assert ("lstat", path) in later, path
            probe = later.index(("lstat", path))
            assert not any(k == "mkdir" and path in p.parents for k, p in later[:probe]), path
        elif kind == "write":
            assert ("lstat", path.parent) in events[:index], path
    assert events[-1] == ("audit", staging)


def test_write_failure_discards_staging_and_names_the_artifact(store: Path, monkeypatch) -> None:
    real_write, calls = installation.atomic_write_bytes, []

    def write(path, content):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real_write(path, content)

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    _fails(
        "The generation artifact could not be staged: store/decisions/001-x.md.",
        lambda: stage_generation_root(_projection()),
    )
    actor = _actor(store)
    assert (entry_names(actor / "staging"), entry_names(actor / "generations")) == (set(), set())


@pytest.mark.parametrize("failure", ["typed", "crash"])
def test_audit_failure_discards_only_on_typed_refusal(store: Path, monkeypatch, failure: str):
    def is_tmp_sibling(name: str) -> bool:
        if failure == "crash":
            raise RuntimeError("walk interrupted")
        return True

    monkeypatch.setattr(installation, "is_tmp_sibling", is_tmp_sibling)
    actor = _actor(store)
    if failure == "typed":
        with pytest.raises(GenerationInstallError, match="holds a partial write"):
            stage_generation_root(_projection())
        assert entry_names(actor / "staging") == set()
    else:
        with pytest.raises(RuntimeError, match="walk interrupted"):
            stage_generation_root(_projection())
        (survivor,) = entry_names(actor / "staging")
        assert "store/questions-provenance.json" in _tree(actor / "staging" / survivor)
    assert entry_names(actor / "generations") == set()


def test_module_is_dormant_and_private(store: Path, monkeypatch) -> None:
    source = Path(installation.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported.isdisjoint(FORBIDDEN_IMPORTS)
    assert not [name for name in SINGLE_CALLER_NAMES if name in source]
    assert list(signature(stage_generation_root).parameters) == ["projection"]
    assert [item.name for item in fields(StagedGenerationRoot)] == (
        "target root_key root_path staging_path audit".split()
    )
    assert [item.name for item in fields(GenerationRootAudit)] == (
        "manifest_digest artifact_count byte_total".split()
    )
    assert issubclass(GenerationRootDivergedError, GenerationAuthorityError)
    assert GenerationRootDivergedError.code == "generation_root_diverged"
    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if os.path.isdir(source):
            pytest.fail(f"directory rename {source}")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(installation.os, "replace", replace)
    for name in ("locked_replica_control_snapshot", "_native_control_lock"):
        monkeypatch.setattr(replica_control, name, lambda *_a, **_k: pytest.fail("lock path"))
    monkeypatch.setattr(
        installation, "_native_control_lock", lambda *_a, **_k: pytest.fail("lock path")
    )
    staged = stage_generation_root(_projection())
    with pytest.raises(FrozenInstanceError):
        staged.root_key = "x"


def test_publish_then_reuse_without_writing(store: Path, monkeypatch) -> None:
    before = entry_names(store)
    projection = _projection()
    root = _generations(store) / ROOT_KEY
    expected = {f"store/{path}": content for path, content in ARTIFACTS.items()}
    expected["manifest.json"] = projection.manifest_json
    installed = install_generation_root(projection)
    audit = GenerationRootAudit(
        projection.target.identity.manifest_digest,
        len(ARTIFACTS),
        sum(len(content) for content in expected.values()),
    )
    assert installed == InstalledGenerationRoot(projection.target, ROOT_KEY, root, audit, False)
    assert _tree(root) == sorted(expected.keys() | {"store", "store/context", "store/decisions"})
    assert _bytes(root) == expected
    assert (entry_names(_staging(store)), entry_names(_generations(store))) == (set(), {ROOT_KEY})
    assert entry_names(store) - {LOCK_NAME} == before | {".replica"}
    monkeypatch.setattr(installation, "atomic_write_bytes", lambda *_a: pytest.fail("wrote"))
    again = install_generation_root(projection)
    assert again == InstalledGenerationRoot(projection.target, ROOT_KEY, root, audit, True)
    assert _bytes(root) == expected
    assert (entry_names(_staging(store)), entry_names(_generations(store))) == (set(), {ROOT_KEY})
    _fails(
        "Generation installation requires a verified projection.",
        lambda: install_generation_root(object()),
    )


@pytest.mark.parametrize(("mutation", "message"), AUDIT_ROWS)
def test_divergent_root_is_refused_in_place(store, tmp_path, monkeypatch, mutation, message):
    if mutation in LINK_KINDS:
        _require_link_kind(mutation)
    projection = _projection()
    root = install_generation_root(projection).root_path
    _mutate(root, mutation, projection, tmp_path, monkeypatch)
    before = _bytes(root)
    monkeypatch.setattr(installation, "atomic_write_bytes", lambda *_a: pytest.fail("wrote"))
    _diverges(message, lambda: install_generation_root(projection))
    assert _bytes(root) == before
    assert (entry_names(_staging(store)), entry_names(_generations(store))) == (set(), {ROOT_KEY})


def test_root_path_occupied_by_a_file_is_refused_and_kept(store: Path) -> None:
    root = _generations(store) / ROOT_KEY
    root.parent.mkdir(parents=True)
    root.write_bytes(b"file\n")
    _diverges(OCCUPIED, lambda: install_generation_root(_projection()))
    assert (root.read_bytes(), entry_names(_staging(store))) == (b"file\n", set())


@pytest.mark.parametrize("kind", [*LINK_KINDS, "dangling"])
def test_linked_root_path_is_refused_before_any_kind_check(store, tmp_path, kind) -> None:
    _require_link_kind("symlink" if kind == "dangling" else kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    root = _generations(store) / ROOT_KEY
    _staging(store).mkdir(parents=True)
    root.parent.mkdir()
    if kind == "dangling":
        root.symlink_to(outside / "absent")
    else:
        _link(root, outside, kind)
    before = (_tree(outside), _tree(store))
    with pytest.raises(ReplicaControlReadError) as raised:
        install_generation_root(_projection())
    assert raised.value.code == "generation_control_unavailable"
    assert (_tree(outside), _tree(store)) == before


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_absent_probe_sees_a_dangling_link_as_present(tmp_path: Path, kind: str) -> None:
    _require_link_kind(kind)
    assert installation._lstat_optional(tmp_path / "absent") is None
    target = tmp_path / "target"
    target.mkdir()
    dangling = tmp_path / "dangling"
    _link(dangling, target, kind)
    target.rmdir()
    assert not dangling.exists()
    metadata = installation._lstat_optional(dangling)
    assert metadata is not None and _is_link_or_reparse(metadata)


def _plant(store: Path, divergent: bool) -> dict[str, bytes]:
    (name,) = entry_names(_staging(store))
    root = _generations(store) / ROOT_KEY
    shutil.copytree(_staging(store) / name, root)
    if divergent:
        (root / "store" / "decisions" / "001-x.md").write_bytes(b"# 002\n")
    return _bytes(root)


def _wrap_lock(monkeypatch, before_yield, events: list[str] | None = None) -> None:
    real = installation._native_control_lock
    calls = 0

    @contextmanager
    def wrapped(store_path, path, timeout):
        nonlocal calls
        calls += 1
        if calls != 1:
            pytest.fail("publication acquired a second native lock")
        with real(store_path, path, timeout):
            if events is not None:
                events.append("lock-entered")
            before_yield()
            yield
        if events is not None:
            events.append("lock-released")

    monkeypatch.setattr(installation, "_native_control_lock", wrapped)


@pytest.mark.parametrize("divergent", [False, True], ids=["identical", "divergent"])
def test_root_planted_under_the_lock_is_audited_never_replaced(store, monkeypatch, divergent):
    projection = _projection()
    planted: dict[str, bytes] = {}
    _wrap_lock(monkeypatch, lambda: planted.update(_plant(store, divergent)))
    if divergent:
        message = "The generation artifact diverges: decisions/001-x.md."
        _diverges(message, lambda: install_generation_root(projection))
    else:
        assert install_generation_root(projection).reused is True
    assert _bytes(_generations(store) / ROOT_KEY) == planted
    assert (entry_names(_staging(store)), entry_names(_generations(store))) == (set(), {ROOT_KEY})


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_root_link_planted_under_the_lock_is_refused(store, tmp_path, monkeypatch, kind):
    _require_link_kind(kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    root = _generations(store) / ROOT_KEY
    planted: dict[str, os.stat_result] = {}

    def plant() -> None:
        assert len(entry_names(_staging(store))) == 1
        _link(root, outside, kind)
        planted["metadata"] = os.lstat(root)

    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if os.path.isdir(source):
            pytest.fail(f"directory rename {source}")
        return real_replace(source, destination, *args, **kwargs)

    before = _tree(outside)
    monkeypatch.setattr(installation.os, "replace", replace)
    _wrap_lock(monkeypatch, plant)
    with pytest.raises(ReplicaControlReadError) as raised:
        install_generation_root(_projection())
    assert raised.value.code == "generation_control_unavailable"
    assert os.lstat(root) == planted["metadata"]
    assert _is_link_or_reparse(os.lstat(root))
    assert root.samefile(outside)
    assert _tree(outside) == before
    assert entry_names(_staging(store)) == set()


def test_publish_failure_removes_staging_and_publishes_nothing(store: Path, monkeypatch) -> None:
    real = os.replace

    def replace(source, destination, *args, **kwargs):
        if os.path.isdir(source):
            raise OSError("rename refused")
        return real(source, destination, *args, **kwargs)

    monkeypatch.setattr(installation.os, "replace", replace)
    _fails(PUBLISH_FAILED, lambda: install_generation_root(_projection()))
    assert (entry_names(_staging(store)), entry_names(_generations(store))) == (set(), set())


def test_busy_control_lock_publishes_nothing(store: Path) -> None:
    with FileLock(str(store / LOCK_NAME)):
        with pytest.raises(ReplicaControlBusyError) as raised:
            install_generation_root(_projection(), timeout=0.1)
    assert raised.value.code == "generation_control_busy"
    assert (entry_names(_staging(store)), entry_names(_generations(store))) == (set(), set())


@pytest.mark.parametrize("planted", [False, True], ids=["publish", "planted"])
def test_publication_order(store: Path, monkeypatch, planted: bool) -> None:
    events: list[str] = []
    root, staging_dir = _generations(store) / ROOT_KEY, _staging(store)
    real_lstat, real_mkdir, real_replace, real_rmtree = (
        os.lstat,
        os.mkdir,
        os.replace,
        shutil.rmtree,
    )
    real_write, real_audit = installation.atomic_write_bytes, installation.audit_generation_tree

    def lstat(path, *args, **kwargs):
        if Path(path) == root:
            events.append("probe")
        return real_lstat(path, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        if staging_dir in Path(path).parents:
            events.append("create")
        return real_mkdir(path, *args, **kwargs)

    def write(path, content):
        events.append("write")
        real_write(path, content)

    def audit(directory, projection):
        events.append("audit:root" if directory == root else "audit:staging")
        return real_audit(directory, projection)

    def replace(source, destination, *args, **kwargs):
        if os.path.isdir(source):
            events.append("rename")
        return real_replace(source, destination, *args, **kwargs)

    def rmtree(path, *args, **kwargs):
        events.append("remove")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(installation.os, "lstat", lstat)
    monkeypatch.setattr(installation.os, "mkdir", mkdir)
    monkeypatch.setattr(installation.os, "replace", replace)
    monkeypatch.setattr(installation.shutil, "rmtree", rmtree)
    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "audit_generation_tree", audit)
    _wrap_lock(monkeypatch, (lambda: _plant(store, False)) if planted else (lambda: None), events)
    assert install_generation_root(_projection()).reused is planted
    if planted:
        expected = (
            "probe create write audit:staging lock-entered audit:root lock-released remove".split()
        )
        assert "rename" not in events
    else:
        expected = "probe create write audit:staging lock-entered rename lock-released".split()
        assert "remove" not in events
    positions = [events.index(kind) for kind in expected]
    assert positions == sorted(positions), events


SWEEP_ROWS = [
    pytest.param(
        entry,
        kind,
        id=f"{entry}-{kind}" if kind else entry,
        marks=POSIX_ONLY if entry == "fifo" else (),
    )
    for entry, kind in [
        ("stale", None),
        ("fresh", None),
        ("file", None),
        ("fifo", None),
        ("link", "symlink"),
        ("link", "junction"),
        ("holding-link", "symlink"),
        ("holding-link", "junction"),
    ]
]


@pytest.mark.parametrize(("entry", "kind"), SWEEP_ROWS)
def test_sweep_removes_only_stale_plain_directories(store, tmp_path, monkeypatch, entry, kind):
    if kind is not None:
        _require_link_kind(kind)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.md").write_bytes(b"victim\n")
    stale = _staging(store) / f"{ROOT_KEY}-{'0' * 16}"
    if entry in ("link", "file", "fifo"):
        stale.parent.mkdir(parents=True)
        if entry == "link":
            _link(stale, outside, kind)
        elif entry == "file":
            stale.write_bytes(b"stray\n")
        else:
            os.mkfifo(stale)
        monkeypatch.setattr(installation.shutil, "rmtree", lambda *_a, **_k: pytest.fail("rmtree"))
    else:
        (stale / "store").mkdir(parents=True)
        if entry == "holding-link":
            _link(stale / "store" / "context", outside, kind)
    if entry == "link":
        planted_mtime = os.lstat(stale).st_mtime
        monkeypatch.setattr(installation.time, "time", lambda: planted_mtime + 7200)
    else:
        _age(stale, 600 if entry == "fresh" else 7200)
    before = _tree(outside)
    assert install_generation_root(_projection()).reused is False
    survivors = {stale.name} if entry in ("fresh", "file", "fifo", "link") else set()
    assert entry_names(_staging(store)) == survivors
    assert _tree(outside) == before


def test_sweep_and_reuse_never_list_generations(store: Path, monkeypatch) -> None:
    real, generations = os.scandir, _generations(store)

    def scandir(path=".", *args, **kwargs):
        if Path(path) == generations:
            pytest.fail("listed generations")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(installation.os, "scandir", scandir)
    assert [install_generation_root(_projection()).reused for _ in range(2)] == [False, True]


def _without_reason(envelope: dict) -> dict:
    shape = dict(envelope)
    shape["error"] = {key: value for key, value in shape["error"].items() if key != "reason"}
    return shape


def test_installed_root_coexists_with_legacy_surfaces(tmp_path: Path) -> None:
    store = _scaffolded_cloud_project("nauro", tmp_path, PROJECT_ID)
    _seed_token()
    assert store == get_store_path_v2(PROJECT_ID)
    (store / "context").mkdir(exist_ok=True)
    projection = _projection()
    installed = install_generation_root(projection)
    root = installed.root_path
    authority = publish_generation_control(installed)
    before = _bytes(root)
    walked = [
        entry.raw_relative_path
        for entry in merge._walk_store_files(merge._prepare_store_root(store))
    ]
    assert walked
    assert not [path for path in walked if Path(path).parts[0].casefold() == ".replica"]
    envelope = tool_get_raw_file(store, f"{ACTOR}/generations/{ROOT_KEY}/manifest.json")
    assert _without_reason(envelope) == _without_reason(
        tool_get_raw_file(store, "does-not-exist.md")
    )
    assert not [name for name in envelope["available_files"] if ".replica" in name.casefold()]
    report, reporter = pull_report(
        store, [("context/note.md", b"note\n"), (".replica/authority.json", b"x")]
    )
    assert (report, reporter.warns) == (PullReport(merged=1), [])
    assert (store / "context" / "note.md").read_bytes() == b"note\n"
    assert entry_names(store / ".replica") == {"authority.json", "v1"}
    assert _bytes(root) == before
    assert DecisionCorpus.scan(store).usable is True
    assert list_conflict_backup_files(store) == []
    assert list_quarantine_backups(store) == []
    assert ".replica" not in json.dumps(tool_check_decision(store, "Keep project intent stable"))
    with locked_replica_control_snapshot(
        projection.target.binding, active_user_id=USER_ID, active_projection_scope_id=SCOPE_ID
    ) as snapshot:
        assert snapshot.authority == authority


def test_installer_is_dormant_and_private(store: Path, monkeypatch) -> None:
    assert list(signature(install_generation_root).parameters) == ["projection", "timeout"]
    assert [item.name for item in fields(InstalledGenerationRoot)] == (
        "target root_key root_path audit reused".split()
    )
    assert list(signature(publish_generation_control).parameters) == ["installed", "timeout"]
    assert "locked_replica_control_snapshot" not in Path(installation.__file__).read_text()
    for path in Path(installation.__file__).parents[2].rglob("*.py"):
        if path != Path(installation.__file__):
            assert "publish_generation_control(" not in path.read_text(encoding="utf-8")
    monkeypatch.setattr("nauro.sync.state.save_state", lambda *_a, **_k: pytest.fail("state"))
    installed = install_generation_root(_projection())
    with pytest.raises(FrozenInstanceError):
        installed.reused = True


def test_control_publication_writes_carrier_pointer_then_marker(store: Path, monkeypatch) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    writes: list[tuple[Path, bytes]] = []
    real_write = installation.atomic_write_bytes

    def write(path: Path, content: bytes) -> None:
        writes.append((path, content))
        real_write(path, content)

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    monkeypatch.setattr(installation, "datetime", FIXED_CLOCK)

    authority = publish_generation_control(installed)

    assert [path for path, _content in writes] == [carrier, pointer, marker]
    assert carrier.read_bytes() == _authorization_bytes(
        projection, installed_state_id=INSTALLED_STATE_ID
    )
    assert authority.pointer.installed_state_id == INSTALLED_STATE_ID
    assert pointer.read_bytes() == authority.pointer.canonical_bytes()


@pytest.mark.parametrize("state", ["absent", "malformed", "noncanonical", "divergent"])
def test_control_publication_mints_after_lock_and_audit_before_ordered_writes(
    store: Path, monkeypatch, state: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer_path = _control_files(store)
    carrier_path = _authorization_view(store)
    if state != "absent":
        raw = _pointer_bytes(projection)
        if state == "malformed":
            raw = b"{"
        elif state == "noncanonical":
            raw = json.dumps(json.loads(raw)).encode()
        elif state == "divergent":
            raw = _pointer_bytes(projection, generation_id="01K55555555555555555555555")
        pointer_path.write_bytes(raw)
    events: list[str] = []
    real_audit = installation._audit_installed_root
    real_stream = installation._stream_digest
    real_parse = installation._parse_manifest
    real_read = installation._read_control_file
    real_write = installation.atomic_write_bytes
    real_select = installation.select_project_authority
    real_mkdir = Path.mkdir

    def audit(*args):
        result = real_audit(*args)
        assert result == COMMITTED_AT
        events.append("audit-complete")
        return result

    def stream(*args):
        result = real_stream(*args)
        if args[4] == "manifest.json":
            events.append("manifest-digest-verified")
        return result

    def parse(*args):
        assert events[-1] == "manifest-digest-verified"
        result = real_parse(*args)
        events.append("manifest-committed-at-bound")
        return result

    def read(store_path, path):
        result = real_read(store_path, path)
        if "marker-write" in events and path in (marker, pointer_path, carrier_path):
            events.append("marker-read" if path == marker else "final-read")
        elif path == carrier_path and "carrier-write" in events:
            events.append("carrier-read")
        elif path == pointer_path and "pointer-write" in events:
            events.append("pointer-read")
        return result

    def write(path, content):
        event = {
            carrier_path: "carrier-write",
            pointer_path: "pointer-write",
            marker: "marker-write",
        }[path]
        assert events[-1] == "actor"
        if event == "marker-write":
            assert events.count("pointer-read") >= 2
        events.append(event)
        real_write(path, content)

    def mint() -> str:
        assert events.count("lock-entered") == 1
        assert events.count("audit-complete") == 1
        assert "carrier-write" not in events
        events.append("ulid")
        return INSTALLED_STATE_ID

    def select(*args, **kwargs):
        if "marker-write" in events:
            events.append("final-select")
        return real_select(*args, **kwargs)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            assert events.count("lock-entered") == 1
            assert events.count("audit-complete") == 1
            assert "carrier-write" not in events
            events.append("clock")
            return cls(2026, 9, 4, 1, 2, 3, 4, tzinfo=tz)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if self in (pointer_path.parent, marker.parent) and parents:
            metadata = os.lstat(self)
            assert stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(metadata)
            events.append("pointer-parent" if self == pointer_path.parent else "marker-parent")
        return real_mkdir(self, mode, parents, exist_ok)

    _wrap_lock(monkeypatch, lambda: None, events)
    monkeypatch.setattr(installation, "_audit_installed_root", audit)
    monkeypatch.setattr(installation, "_stream_digest", stream)
    monkeypatch.setattr(installation, "_parse_manifest", parse)
    monkeypatch.setattr(installation, "_read_control_file", read)
    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", mint, raising=False)
    monkeypatch.setattr(installation, "datetime", Clock)
    monkeypatch.setattr(installation, "select_project_authority", select)
    monkeypatch.setattr(
        installation,
        "read_active_user_id",
        lambda: events.append("actor") or USER_ID,
    )
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(
        replica_control,
        "locked_replica_control_snapshot",
        lambda *_a, **_k: pytest.fail("publication nested a locked snapshot"),
    )

    authority = installation.publish_generation_control(installed)

    assert (authority.pointer.installed_at, authority.pointer.installed_state_id) == (
        INSTALLED_AT,
        INSTALLED_STATE_ID,
    )
    assert events.count("lock-entered") == 1
    assert events.count("audit-complete") == 3
    assert events.count("clock") == events.count("ulid") == 1
    proof_order = "manifest-digest-verified manifest-committed-at-bound audit-complete".split()
    assert [events.index(item) for item in proof_order] == sorted(map(events.index, proof_order))
    assert events.index("audit-complete") < events.index("clock") < events.index("pointer-write")
    assert events.index("audit-complete") < events.index("ulid") < events.index("pointer-write")
    order = (
        "carrier-write carrier-read pointer-write pointer-read marker-write marker-read "
        "final-read final-select"
    ).split()
    assert [events.index(item) for item in order] == sorted(map(events.index, order))
    assert events.count("pointer-parent") == 2
    assert events.count("marker-parent") == 1
    assert events[-2:] == ["lock-released", "actor"]


def test_matching_dormant_pointer_is_reused_without_mint_or_rewrite(
    store: Path, monkeypatch
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    dormant = _pointer_bytes(projection)
    pointer.write_bytes(dormant)
    carrier.write_bytes(_authorization_bytes(projection))
    real_write = installation.atomic_write_bytes

    def write(path, content):
        if path in (carrier, pointer):
            pytest.fail("dormant pair rewritten")
        real_write(path, content)

    events: list[str] = []
    _wrap_lock(monkeypatch, lambda: None, events)
    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", lambda: pytest.fail("minted"))
    monkeypatch.setattr(installation, "datetime", _FailDateTime)
    authority = publish_generation_control(installed)
    assert (carrier.read_bytes(), pointer.read_bytes(), marker.read_bytes()) == (
        _authorization_bytes(projection),
        dormant,
        authority.marker.canonical_bytes(),
    )
    assert authority.pointer.installed_state_id == OTHER_STATE_ID
    assert events == ["lock-entered", "lock-released"]


def test_active_repeat_reaudits_without_mint_or_write(store: Path, monkeypatch) -> None:
    installed = install_generation_root(_projection())
    expected = publish_generation_control(installed)
    events: list[str] = []
    _wrap_lock(monkeypatch, lambda: None, events)
    _forbid_publication_effects(monkeypatch)
    assert publish_generation_control(installed) == expected
    assert events == ["lock-entered", "lock-released"]


@pytest.mark.parametrize("branch", ["absent", "dormant-replacement", "active"])
def test_changed_only_target_commit_time_fails_before_publication_effects(
    store: Path, monkeypatch, branch: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    if branch == "dormant-replacement":
        pointer.write_bytes(b"{")
    elif branch == "active":
        publish_generation_control(installed)
        pointer.write_bytes(_pointer_bytes(projection, committed_at=OTHER_COMMITTED_AT))
    identity = installed.target.identity.model_copy(update={"committed_at": OTHER_COMMITTED_AT})
    target = GenerationProjectionTarget(installed.target.binding, identity)
    forged = replace(installed, target=target)
    before = tuple(path.read_bytes() if path.exists() else None for path in (marker, pointer))
    _forbid_publication_effects(monkeypatch)

    if branch == "active":
        with pytest.raises(RefreshRequiredError) as raised:
            publish_generation_control(forged)
        assert str(raised.value) == "The installed replica requires a fresh authorization view."
    else:
        _diverges(
            "The generation manifest does not match the projection target.",
            lambda: publish_generation_control(forged),
        )

    assert (
        tuple(path.read_bytes() if path.exists() else None for path in (marker, pointer)) == before
    )


def test_same_root_with_another_bound_commit_time_is_not_reused(store: Path) -> None:
    original = install_generation_root(_projection())
    changed = _projection(committed_at=OTHER_COMMITTED_AT)
    assert installation._root_key(changed.target) == original.root_key
    _diverges(
        "The generation root manifest diverges.",
        lambda: install_generation_root(changed),
    )
    assert (original.root_path / "manifest.json").read_bytes() == _projection().manifest_json


def test_pre_binding_installed_manifest_fails_closed_without_migration(
    store: Path, monkeypatch
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    manifest_path = installed.root_path / "manifest.json"
    body = json.loads(projection.manifest_json)
    body.pop("committed_at")
    old_manifest = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    manifest_path.write_bytes(old_manifest)
    old_digest = hashlib.sha256(old_manifest).hexdigest()
    identity = installed.target.identity.model_copy(update={"manifest_digest": old_digest})
    target = GenerationProjectionTarget(installed.target.binding, identity)
    audit = replace(
        installed.audit,
        manifest_digest=old_digest,
        byte_total=installed.audit.byte_total - len(projection.manifest_json) + len(old_manifest),
    )
    forged = replace(installed, target=target, audit=audit)
    before = _bytes(installed.root_path)
    _forbid_publication_effects(monkeypatch)

    _diverges("The generation manifest is malformed.", lambda: publish_generation_control(forged))

    assert _bytes(installed.root_path) == before
    assert all(not os.path.lexists(path) for path in _control_files(store))


@pytest.mark.parametrize(
    "case",
    "root-key root-path audit-digest audit-count audit-total reused-type target-subclass "
    "target-partial target-extra binding-malformed identity-malformed identity-stale".split(),
)
def test_forged_installed_root_is_rederived_or_rejected(
    store: Path, tmp_path: Path, monkeypatch, case: str
) -> None:
    installed = install_generation_root(_projection())
    completed, real_stream = [], installation._stream_digest

    def stream(*args):
        result = real_stream(*args)
        completed.append(args[4])
        return result

    monkeypatch.setattr(installation, "_stream_digest", stream)
    target = installed.target
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_bytes(b"victim")
    if case == "root-key":
        forged = replace(installed, root_key="0" * 32)
    elif case == "root-path":
        forged = replace(installed, root_path=outside)
    elif case.startswith("audit-"):
        field = {"audit-digest": "manifest_digest", "audit-count": "artifact_count"}.get(
            case, "byte_total"
        )
        value = "b" * 64 if field == "manifest_digest" else getattr(installed.audit, field) + 1
        forged = replace(installed, audit=replace(installed.audit, **{field: value}))
    elif case == "reused-type":
        forged = replace(installed, reused=1)
    else:
        if case == "target-subclass":

            class TargetSubclass(GenerationProjectionTarget):
                pass

            bad_target = object.__new__(TargetSubclass)
        else:
            bad_target = object.__new__(GenerationProjectionTarget)
        if case != "target-partial":
            binding = target.binding
            if case == "binding-malformed":
                binding = replace(binding, project_id=1)
            update = {"project_id": 1} if case == "identity-malformed" else {}
            if case == "identity-stale":
                update = {"generation_id": "01K55555555555555555555555"}
            identity = target.identity.model_copy(update=update)
            object.__setattr__(bad_target, "binding", binding)
            object.__setattr__(bad_target, "identity", identity)
            if case == "target-extra":
                object.__setattr__(bad_target, "extra", True)
        forged = replace(installed, target=bad_target)
    _forbid_publication_effects(monkeypatch)
    with pytest.raises(GenerationControlPublicationError) as raised:
        publish_generation_control(forged)
    assert raised.value.code == "generation_control_publication_failed"
    assert _control_files(store)[0].exists() is False
    assert (outside / "victim").read_bytes() == b"victim"
    if case.startswith("audit-"):
        assert completed == ["manifest.json", *sorted(ARTIFACTS)]


def test_valid_reused_flag_is_irrelevant(tmp_path: Path, monkeypatch) -> None:
    fixed = datetime.fromisoformat(INSTALLED_AT.replace("Z", "+00:00"))
    real_read, real_write = installation._read_control_file, installation.atomic_write_bytes

    def run(reused: bool):
        monkeypatch.setenv("NAURO_HOME", str(tmp_path / str(reused)))
        monkeypatch.setattr(installation, "atomic_write_bytes", real_write)
        local_store, projection = get_store_path_v2(PROJECT_ID), _projection()
        scaffold_project_store("nauro", local_store)
        installed = install_generation_root(projection)
        installed = install_generation_root(projection) if reused else installed
        assert installed.reused is reused
        seen = []
        clock = type("Clock", (), {"now": classmethod(lambda *_a: seen.append("clock") or fixed)})
        ids = iter([INSTALLED_STATE_ID])
        monkeypatch.setattr(installation, "datetime", clock)
        monkeypatch.setattr(installation, "generate_ulid", lambda: seen.append("ulid") or next(ids))

        def read(store_path, path):
            seen.append(("read", path.relative_to(local_store).as_posix()))
            return real_read(store_path, path)

        def write(path, content):
            seen.append(("write", path.relative_to(local_store).as_posix()))
            real_write(path, content)

        monkeypatch.setattr(installation, "_read_control_file", read)
        monkeypatch.setattr(installation, "atomic_write_bytes", write)
        authority = publish_generation_control(installed)
        control = tuple(path.read_bytes() for path in _control_files(local_store))
        expected = authority.marker.canonical_bytes(), authority.pointer.canonical_bytes()
        assert control == expected
        assert control[1] == _pointer_bytes(projection, installed_state_id=INSTALLED_STATE_ID)
        return control, seen, type(authority)

    assert run(False) == run(True)


def test_installed_proof_changed_while_waiting_is_rejected(store: Path, monkeypatch) -> None:
    installed = install_generation_root(_projection())
    _wrap_lock(monkeypatch, lambda: object.__setattr__(installed, "root_key", "0" * 32))
    _forbid_publication_effects(monkeypatch)
    with pytest.raises(GenerationControlPublicationError):
        publish_generation_control(installed)
    assert _control_files(store)[0].exists() is False


def test_control_publication_refuses_a_busy_native_lock(store: Path, monkeypatch) -> None:
    installed = install_generation_root(_projection())
    _forbid_publication_effects(monkeypatch)
    with FileLock(str(store / LOCK_NAME)):
        with pytest.raises(ReplicaControlBusyError) as raised:
            publish_generation_control(installed, timeout=0.1)
    assert raised.value.code == "generation_control_busy"
    assert not any(map(os.path.lexists, (*_control_files(store), _authorization_view(store))))


@pytest.mark.parametrize("timing", ["before", "locked", "active"])
@pytest.mark.parametrize("boundary", ["lock", "carrier", "pointer", "marker"])
@pytest.mark.parametrize(
    "kind",
    "symlink dangling junction hardlink directory fifo device socket oversized unreadable".split(),
)
def test_control_publication_preserves_unsafe_control_occupants(
    store: Path, tmp_path: Path, monkeypatch, boundary: str, kind: str, timing: str
) -> None:
    if boundary == "lock" and timing == "locked":
        pytest.skip("leaf timing applies to pointer and marker")
    if timing == "active" and boundary not in {"carrier", "pointer"}:
        pytest.skip("active marker proof applies to actor control files")
    if boundary == "lock" and kind in {"dangling", "fifo", "oversized"}:
        pytest.skip("not a lock occupant row")
    _guard_occupant(kind)
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    path = {
        "lock": store / LOCK_NAME,
        "carrier": _authorization_view(store),
        "pointer": pointer,
        "marker": marker,
    }[boundary]
    if timing == "active":
        publish_generation_control(installed)
    if boundary == "lock":
        path.unlink()
    if boundary == "marker":
        pointer.write_bytes(b"keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_bytes(b"victim")
    planted = []

    def plant() -> None:
        if timing == "active":
            path.unlink()
        _plant_occupant(path, kind, outside)
        planted.append(_fingerprint(path))

    plant() if timing != "locked" else _wrap_lock(monkeypatch, plant)
    if kind in {"device", "socket"}:
        real_lstat = os.lstat

        def lstat(candidate):
            value = real_lstat(candidate)
            if Path(candidate) == path:
                fields, attributes = value.__reduce__()[1]
                fields = list(fields)
                fields[0] = stat.S_IFCHR if kind == "device" else stat.S_IFSOCK
                return os.stat_result(fields, attributes)
            return value

        monkeypatch.setattr(installation.os, "lstat", lstat)
        if timing != "locked":
            planted[0] = _fingerprint(path)
    if boundary == "carrier":
        _forbid_pointer_access(monkeypatch, pointer)
    _forbid_publication_effects(monkeypatch)
    message = (
        "Replica control paths cannot contain links."
        if kind in (*LINK_KINDS, "dangling")
        else "Replica control lock path is unsafe."
        if boundary == "lock" and kind != "unreadable"
        else "Replica control lock is unavailable."
        if boundary == "lock"
        else "Replica control file is unreadable."
        if kind == "unreadable"
        else "Replica control file is not a bounded regular file."
    )
    _assert_control_error(
        ReplicaControlReadError,
        "generation_control_unavailable",
        message,
        lambda: publish_generation_control(installed),
    )
    assert _fingerprint(path) == planted[0]
    assert (outside / "victim").read_bytes() == b"victim"
    if boundary == "marker":
        assert pointer.read_bytes() == b"keep"
    elif timing == "active":
        assert marker.exists()
    else:
        assert not os.path.lexists(marker)


@pytest.mark.parametrize("timing", ["before", "locked"])
@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "junction", "fifo"])
@pytest.mark.parametrize(
    "boundary",
    "store control version actors actor generations root pointer-parent marker-parent".split(),
)
def test_control_publication_refuses_path_substitution(
    store: Path, tmp_path: Path, monkeypatch, boundary: str, kind: str, timing: str
) -> None:
    _guard_occupant(kind)
    if sys.platform == "win32" and timing == "locked" and boundary == "store":
        pytest.skip("Windows holds the lock path open")
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    pointer.write_bytes(_pointer_bytes(projection))
    actor = _actor(store)
    path = {
        "store": store,
        "control": store / ".replica",
        "version": store / ".replica" / "v1",
        "actors": actor.parent,
        "actor": actor,
        "generations": actor / "generations",
        "root": installed.root_path,
        "pointer-parent": pointer.parent,
        "marker-parent": marker.parent,
    }[boundary]
    backup = tmp_path / "original"
    saved_pointer = backup / pointer.relative_to(path) if pointer.is_relative_to(path) else pointer
    saved_marker = backup / marker.relative_to(path) if marker.is_relative_to(path) else marker

    def substitute() -> None:
        path.rename(backup)
        (backup / "victim").write_bytes(b"victim")
        _plant_occupant(path, kind, backup)

    if timing == "before":
        substitute()
    else:
        _wrap_lock(monkeypatch, substitute)
    _forbid_publication_effects(monkeypatch)
    with pytest.raises((ReplicaControlReadError, GenerationRootDivergedError)) as raised:
        publish_generation_control(installed)
    root_occupant = boundary == "root" and kind in {"missing", "file", "fifo"}
    assert raised.value.code == (DIVERGED if root_occupant else "generation_control_unavailable")
    assert _fingerprint(path) is None if kind == "missing" else _fingerprint(path) is not None
    assert saved_pointer.read_bytes() == _pointer_bytes(projection)
    assert not os.path.lexists(saved_marker)
    assert (backup / "victim").read_bytes() == b"victim"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "01K00000000000000000000000"),
        ("store_format_version", 2),
        ("generation_id", "01K55555555555555555555555"),
        ("manifest_digest", "b" * 64),
        ("committed_at", "2998-12-31T23:59:59.999999Z"),
        ("installed_for_user_id", "01K44444444444444444444444"),
        ("projection_class", "viewer"),
        ("projection_scope_id", "b" * 64),
    ],
)
@pytest.mark.parametrize("control_name", ["carrier", "pointer"])
def test_active_target_mismatch_never_mutates(
    store: Path, monkeypatch, control_name: str, field: str, value: object
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    publish_generation_control(installed)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    control = carrier if control_name == "carrier" else pointer
    body = json.loads(control.read_bytes())
    body[field] = value
    control.write_bytes(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    before = marker.read_bytes(), carrier.read_bytes(), pointer.read_bytes()
    monkeypatch.setattr(installation, "atomic_write_bytes", lambda *_a: pytest.fail("wrote"))
    expected = (
        ReplicaActorMismatchError if field == "installed_for_user_id" else RefreshRequiredError
    )
    message = (
        "The installed replica belongs to another account."
        if field == "installed_for_user_id"
        else "The installed replica requires a fresh authorization view."
    )
    _assert_control_error(
        expected, expected.code, message, lambda: publish_generation_control(installed)
    )
    assert (marker.read_bytes(), carrier.read_bytes(), pointer.read_bytes()) == before


@pytest.mark.parametrize(
    "state", ["ptr-none", "ptr-bad", "ptr-spaced", "marker-bad", "marker-spaced", "marker-v2"]
)
def test_active_incomplete_or_noncanonical_state_fails_closed(
    store: Path, monkeypatch, state: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    authority = publish_generation_control(installed)
    marker, pointer = _control_files(store)
    if state == "ptr-none":
        pointer.unlink()
    elif state == "ptr-bad":
        pointer.write_bytes(b"{")
    elif state == "ptr-spaced":
        pointer.write_bytes(json.dumps(authority.pointer.model_dump()).encode())
    elif state == "marker-bad":
        marker.write_bytes(b"{")
    elif state == "marker-spaced":
        marker.write_bytes(json.dumps(authority.marker.model_dump()).encode())
    else:
        body = authority.marker.model_dump()
        body["store_format_version"] = 2
        marker.write_bytes(json.dumps(body, separators=(",", ":")).encode())
    before = (marker.read_bytes(), pointer.read_bytes() if pointer.exists() else None)
    monkeypatch.setattr(installation, "atomic_write_bytes", lambda *_a: pytest.fail("wrote"))
    with pytest.raises(GenerationAuthorityError):
        publish_generation_control(installed)
    assert (marker.read_bytes(), pointer.read_bytes() if pointer.exists() else None) == before


@pytest.mark.parametrize("branch", ["absent", "dormant", "active"])
@pytest.mark.parametrize(("mutation", "message"), AUDIT_ROWS)
def test_control_publication_reaudits_every_installed_byte(
    store, tmp_path, monkeypatch, mutation, message, branch
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    if branch == "dormant":
        pointer.write_bytes(_pointer_bytes(projection))
    elif branch == "active":
        publish_generation_control(installed)
    before = tuple(path.read_bytes() if path.exists() else None for path in (marker, pointer))
    if mutation in LINK_KINDS:
        _require_link_kind(mutation)
    _mutate(installed.root_path, mutation, projection, tmp_path, monkeypatch)
    _forbid_publication_effects(monkeypatch)
    _diverges(message, lambda: publish_generation_control(installed))
    assert (
        tuple(path.read_bytes() if path.exists() else None for path in (marker, pointer)) == before
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "carrier-fails",
        "carrier-prior",
        "carrier-landed",
        "carrier-third",
        "carrier-unsafe",
        "carrier-actor",
        "carrier-return-fails",
        "carrier-return-prior",
        "carrier-return-third",
        "carrier-return-unsafe",
        "pointer-fails",
        "pointer-prior",
        "pointer-landed",
        "pointer-third",
        "pointer-unsafe",
        "pointer-actor",
        "pointer-return-fails",
        "pointer-return-prior",
        "pointer-return-third",
        "pointer-return-unsafe",
        "marker-fails",
        "marker-unsafe",
        "marker-landed",
        "marker-corrupt",
    ],
)
def test_control_publication_resolves_crash_visible_boundaries(
    store: Path, monkeypatch, outcome: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    prior_carrier = _authorization_bytes(projection, manifest_digest="b" * 64)
    if outcome.startswith("carrier") and outcome.endswith("prior"):
        carrier.write_bytes(prior_carrier)
    if outcome.startswith("pointer") and outcome.endswith("prior"):
        pointer.write_bytes(b"prior")
    real_write = installation.atomic_write_bytes
    real_read = installation._read_control_file
    failed = False

    def write(path, content):
        nonlocal failed
        stage = "carrier" if path == carrier else "pointer" if path == pointer else "marker"
        if stage != "marker" and outcome.startswith(stage):
            result = outcome.removeprefix(f"{stage}-")
            returns = result.startswith("return-")
            result = result.removeprefix("return-")
            if result in ("landed", "actor"):
                real_write(path, content)
            elif result == "third":
                path.write_bytes(b"third")
            failed = True
            if returns:
                return
            raise OSError(f"{stage} outcome")
        if path == marker:
            if outcome == "marker-fails":
                raise OSError("marker outcome")
            if outcome == "marker-unsafe":
                failed = True
                raise OSError("marker outcome")
            if outcome == "marker-corrupt":
                path.write_bytes(b"{")
                raise OSError("marker outcome")
            if outcome == "marker-landed":
                real_write(path, content)
                raise OSError("marker outcome")
        real_write(path, content)

    def read(store_path, path):
        if failed and outcome.endswith("unsafe") and path in (carrier, pointer, marker):
            raise ReplicaControlReadError("Replica control file changed during read.")
        return real_read(store_path, path)

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "_read_control_file", read)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    monkeypatch.setattr(installation, "datetime", FIXED_CLOCK)
    if outcome.endswith("actor"):
        changed_at, calls = (3 if outcome.startswith("carrier") else 4), 0

        def actor():
            nonlocal calls
            calls += 1
            return INSTALLED_STATE_ID if calls == changed_at else USER_ID

        monkeypatch.setattr(installation, "read_active_user_id", actor)
    if outcome.endswith("landed"):
        assert isinstance(publish_generation_control(installed), GenerationProjectAuthority)
    else:
        expected = (
            ReplicaActorMismatchError
            if outcome.endswith("actor")
            else ReplicaControlReadError
            if outcome.endswith("unsafe")
            else GenerationControlPublicationError
        )
        message = (
            "The installed replica belongs to another account."
            if outcome.endswith("actor")
            else "Replica control file changed during read."
            if outcome.endswith("unsafe")
            else "The generation control state could not be published."
        )
        _assert_control_error(
            expected, expected.code, message, lambda: publish_generation_control(installed)
        )
    if outcome == "marker-corrupt":
        assert marker.read_bytes() == b"{"
    elif not outcome.endswith("landed"):
        assert not marker.exists()
        with locked_replica_control_snapshot(
            projection.target.binding,
            active_user_id=USER_ID,
            active_projection_scope_id=SCOPE_ID,
        ) as snapshot:
            assert isinstance(snapshot.authority, FlatProjectAuthority)
    if outcome.endswith("landed"):
        return
    intended_carrier = _authorization_bytes(projection, installed_state_id=INSTALLED_STATE_ID)
    intended_pointer = _pointer_bytes(projection, installed_state_id=INSTALLED_STATE_ID)
    expected_carrier = (
        b"third"
        if outcome.startswith("carrier") and outcome.endswith("third")
        else prior_carrier
        if outcome.startswith("carrier") and outcome.endswith("prior")
        else None
        if outcome.startswith("carrier") and outcome.endswith(("fails", "unsafe"))
        else intended_carrier
    )
    expected_pointer = (
        b"third"
        if outcome.startswith("pointer") and outcome.endswith("third")
        else b"prior"
        if outcome.startswith("pointer") and outcome.endswith("prior")
        else None
        if outcome.startswith("carrier")
        or outcome.startswith("pointer")
        and outcome.endswith(("fails", "unsafe"))
        else intended_pointer
    )
    expected_marker = b"{" if outcome == "marker-corrupt" else None
    assert tuple(
        path.read_bytes() if path.exists() else None for path in (carrier, pointer, marker)
    ) == (expected_carrier, expected_pointer, expected_marker)
    monkeypatch.setattr(installation, "atomic_write_bytes", real_write)
    monkeypatch.setattr(installation, "_read_control_file", real_read)
    monkeypatch.setattr(installation, "read_active_user_id", lambda: USER_ID)
    if outcome.startswith("carrier") and outcome.endswith("third"):

        def retry():
            return publish_generation_control(installed)

        _assert_control_error(
            GenerationControlCorruptError,
            "generation_control_corrupt",
            "The installed authorization view is invalid.",
            retry,
        )
    elif outcome == "marker-corrupt":
        _assert_control_error(
            GenerationControlCorruptError,
            "generation_control_corrupt",
            "The generation authority marker is invalid.",
            lambda: publish_generation_control(installed),
        )
    else:
        assert isinstance(publish_generation_control(installed), GenerationProjectAuthority)


@pytest.mark.parametrize(
    ("proof", "expected", "message"),
    [
        (
            "carrier-parse",
            GenerationControlCorruptError,
            "The installed authorization view is invalid.",
        ),
        (
            "carrier-canonical",
            GenerationControlCorruptError,
            "The installed authorization view is invalid.",
        ),
        (
            "carrier-actor",
            ReplicaActorMismatchError,
            "The installed replica belongs to another account.",
        ),
        (
            "carrier-nonactor",
            GenerationControlPublicationError,
            "The generation control state could not be published.",
        ),
        (
            "pointer-parse",
            GenerationControlCorruptError,
            "The installed generation pointer is invalid.",
        ),
        (
            "pointer-canonical",
            GenerationControlCorruptError,
            "The installed generation pointer is invalid.",
        ),
        (
            "pointer-actor",
            ReplicaActorMismatchError,
            "The installed replica belongs to another account.",
        ),
        (
            "pointer-nonactor",
            GenerationControlPublicationError,
            "The generation control state could not be published.",
        ),
    ],
)
def test_exact_intended_write_outcome_requires_each_proof(
    store: Path, monkeypatch, proof: str, expected, message: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    stage, check = proof.split("-")
    real_write = installation.atomic_write_bytes
    real_carrier_parse = installation._parse_authorization_view
    real_pointer_parse = installation._parse_pointer
    real_carrier_validate = installation._validate_carrier
    real_pair_validate = installation._validate_control_pair
    after = False

    def write(path, content):
        nonlocal after
        current = "carrier" if path == carrier else "pointer" if path == pointer else "marker"
        real_write(path, content)
        if current == stage:
            after = True
            raise OSError("injected writer failure")

    def parse_carrier(raw):
        value = real_carrier_parse(raw)
        if after and stage == "carrier" and check == "canonical":
            return value.model_copy(update={"manifest_digest": "b" * 64})
        if after and stage == "carrier" and check == "parse":
            raise GenerationControlCorruptError("injected parser failure")
        return value

    def parse_pointer(raw):
        value = real_pointer_parse(raw)
        if after and stage == "pointer" and check == "canonical":
            return value.model_copy(update={"manifest_digest": "b" * 64})
        if after and stage == "pointer" and check == "parse":
            raise GenerationControlCorruptError("injected parser failure")
        return value

    def validate_carrier(*args):
        if after and stage == "carrier" and check == "actor":
            raise ReplicaActorMismatchError("The installed replica belongs to another account.")
        if after and stage == "carrier" and check == "nonactor":
            return False
        return real_carrier_validate(*args)

    def validate_pair(*args):
        if after and stage == "pointer" and check == "actor":
            raise ReplicaActorMismatchError("The installed replica belongs to another account.")
        if after and stage == "pointer" and check == "nonactor":
            return False
        return real_pair_validate(*args)

    def actor():
        if after:
            raise ActiveUserReadError("ambiguous")
        return USER_ID

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "_parse_authorization_view", parse_carrier)
    monkeypatch.setattr(installation, "_parse_pointer", parse_pointer)
    monkeypatch.setattr(installation, "_validate_carrier", validate_carrier)
    monkeypatch.setattr(installation, "_validate_control_pair", validate_pair)
    monkeypatch.setattr(installation, "read_active_user_id", actor)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    monkeypatch.setattr(installation, "datetime", FIXED_CLOCK)
    _assert_control_error(
        expected, expected.code, message, lambda: publish_generation_control(installed)
    )
    assert not marker.exists()
    assert carrier.read_bytes() == _authorization_bytes(
        projection, installed_state_id=INSTALLED_STATE_ID
    )
    assert pointer.exists() is (stage == "pointer")
    if pointer.exists():
        assert pointer.read_bytes() == _pointer_bytes(
            projection, installed_state_id=INSTALLED_STATE_ID
        )
    assert expected is not RefreshRequiredError


@pytest.mark.parametrize("stage", ["carrier", "pointer"])
@pytest.mark.parametrize(
    ("field", "expected", "message"),
    [
        (
            "manifest_digest",
            GenerationControlPublicationError,
            "The generation control state could not be published.",
        ),
        (
            "installed_for_user_id",
            ReplicaActorMismatchError,
            "The installed replica belongs to another account.",
        ),
    ],
)
def test_intended_control_bytes_are_proved_before_their_write(
    store: Path, monkeypatch, stage: str, field: str, expected, message: str
) -> None:
    installed = install_generation_root(_projection())
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    builder_name = f"_build_{stage}"
    real_builder = getattr(installation, builder_name)
    real_write = installation.atomic_write_bytes
    writes = []

    def build(*args):
        value = real_builder(*args)
        changed = "b" * 64 if field == "manifest_digest" else INSTALLED_STATE_ID
        return value.model_copy(update={field: changed})

    def write(path, content):
        writes.append(path)
        real_write(path, content)

    monkeypatch.setattr(installation, builder_name, build)
    with monkeypatch.context() as scoped:
        scoped.setattr(installation, "atomic_write_bytes", write)
        _assert_control_error(
            expected, expected.code, message, lambda: publish_generation_control(installed)
        )
    assert (carrier in writes, pointer in writes, marker in writes) == (
        stage == "pointer",
        False,
        False,
    )


@pytest.mark.parametrize("stage", ["carrier", "pointer"])
@pytest.mark.parametrize("reason", ["missing", "invalid", "unsafe", "ambiguous", "next"])
def test_landed_outcome_requires_available_active_authentication(
    store: Path, monkeypatch, stage: str, reason: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    real_write = installation.atomic_write_bytes
    real_reprove = installation._reprove_publication_paths
    failed = False
    next_check = False

    def write(path, content):
        nonlocal failed
        current = "carrier" if path == carrier else "pointer" if path == pointer else "marker"
        real_write(path, content)
        if current == stage:
            failed = True
            raise OSError("injected writer failure")

    def actor():
        if failed:
            if reason != "next":
                raise ActiveUserReadError(reason)
            return USER_ID
        return INSTALLED_STATE_ID if next_check else USER_ID

    def reprove(*args):
        nonlocal failed, next_check
        result = real_reprove(*args)
        if failed:
            failed = False
            next_check = reason == "next"
        return result

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "read_active_user_id", actor)
    monkeypatch.setattr(installation, "_reprove_publication_paths", reprove)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    _assert_control_error(
        ReplicaActorMismatchError,
        "replica_actor_mismatch",
        "The installed replica belongs to another account."
        if reason == "next"
        else "The installed replica does not match an active account.",
        lambda: publish_generation_control(installed),
    )
    assert not marker.exists()
    assert (carrier.exists(), pointer.exists()) == (True, stage == "pointer")


def test_pointer_outcome_actor_divergence_precedes_raw_carrier_inequality(
    store: Path, monkeypatch
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    real_write = installation.atomic_write_bytes
    real_read = installation._read_control_file
    pointer_failed = False

    def write(path, content):
        nonlocal pointer_failed
        real_write(path, content)
        if path == pointer:
            pointer_failed = True
            raise OSError("injected writer failure")

    def read(store_path, path):
        if pointer_failed and path == carrier:
            return _authorization_bytes(projection, installed_for_user_id=INSTALLED_STATE_ID)
        return real_read(store_path, path)

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "_read_control_file", read)
    _assert_control_error(
        ReplicaActorMismatchError,
        "replica_actor_mismatch",
        "The installed replica belongs to another account.",
        lambda: publish_generation_control(installed),
    )
    assert carrier.exists() and pointer.exists() and not marker.exists()


def _assert_control_error(expected, code: str, message: str, call) -> None:
    with pytest.raises(expected) as raised:
        call()
    assert (raised.value.code, str(raised.value)) == (code, message)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize(
    ("state", "expected_writes", "expected_id"),
    [
        ("carrier-only", "pointer marker", OTHER_STATE_ID),
        ("pointer-only", "carrier pointer marker", INSTALLED_STATE_ID),
        ("state-mismatch", "pointer marker", OTHER_STATE_ID),
        ("actor-pointer", "pointer marker", OTHER_STATE_ID),
        ("malformed-pointer", "pointer marker", OTHER_STATE_ID),
        ("noncanonical-pointer", "pointer marker", OTHER_STATE_ID),
        ("divergent-carrier", "carrier pointer marker", INSTALLED_STATE_ID),
    ],
)
def test_dormant_recovery_is_carrier_anchored(
    store: Path, monkeypatch, state: str, expected_writes: str, expected_id: str
) -> None:
    projection, installed = _projection(), None
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    if state != "pointer-only":
        carrier.write_bytes(
            _authorization_bytes(
                projection,
                **({"manifest_digest": "b" * 64} if state == "divergent-carrier" else {}),
            )
        )
    if state != "carrier-only" and state != "divergent-carrier":
        changes = {}
        if state == "pointer-only":
            changes["installed_for_user_id"] = INSTALLED_STATE_ID
            changes["projection_scope_id"] = "b" * 64
        elif state == "state-mismatch":
            changes["installed_state_id"] = INSTALLED_STATE_ID
        elif state == "actor-pointer":
            changes["installed_for_user_id"] = INSTALLED_STATE_ID
        raw = _pointer_bytes(projection, **changes)
        if state == "malformed-pointer":
            raw = b"{"
        elif state == "noncanonical-pointer":
            raw = json.dumps(json.loads(raw)).encode()
        pointer.write_bytes(raw)
    real_write, writes = installation.atomic_write_bytes, []

    def write(path, content):
        writes.append({carrier: "carrier", pointer: "pointer", marker: "marker"}[path])
        real_write(path, content)

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    monkeypatch.setattr(installation, "datetime", FIXED_CLOCK)
    authority = publish_generation_control(installed)
    view = InstalledAuthorizationView.model_validate_json(carrier.read_bytes())
    assert (writes, view.installed_state_id, authority.pointer.installed_state_id) == (
        expected_writes.split(),
        expected_id,
        expected_id,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "01K00000000000000000000000"),
        ("store_format_version", 2),
        ("generation_id", "01K55555555555555555555555"),
        ("manifest_digest", "b" * 64),
        ("committed_at", OTHER_COMMITTED_AT),
        ("installed_for_user_id", INSTALLED_STATE_ID),
        ("projection_class", "viewer"),
        ("projection_scope_id", "b" * 64),
        ("installed_state_id", INSTALLED_STATE_ID),
    ],
)
def test_exact_dormant_carrier_wins_over_each_safe_pointer_field(
    store: Path, monkeypatch, field: str, value: object
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    carrier.write_bytes(_authorization_bytes(projection))
    pointer.write_bytes(_pointer_bytes(projection, **{field: value}))
    writes = []
    real_write = installation.atomic_write_bytes

    def write(path, content):
        writes.append(path)
        real_write(path, content)

    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", lambda: pytest.fail("minted"))

    authority = publish_generation_control(installed)

    assert writes == [pointer, marker]
    assert carrier.read_bytes() == _authorization_bytes(projection)
    assert authority.pointer.installed_state_id == OTHER_STATE_ID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "01K00000000000000000000000"),
        ("store_format_version", 2),
        ("generation_id", "01K55555555555555555555555"),
        ("manifest_digest", "b" * 64),
        ("committed_at", OTHER_COMMITTED_AT),
        ("projection_class", "viewer"),
        ("projection_scope_id", "b" * 64),
    ],
)
def test_target_divergent_dormant_carrier_starts_a_new_pair_after_pointer_safety(
    store: Path, monkeypatch, field: str, value: object
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    carrier.write_bytes(_authorization_bytes(projection, **{field: value}))
    pointer.write_bytes(b"{")
    real_read = installation._read_control_file
    real_write = installation.atomic_write_bytes
    events = []

    def read(store_path, path):
        if path == pointer:
            events.append("pointer-read")
        return real_read(store_path, path)

    def write(path, content):
        events.append(path)
        real_write(path, content)

    monkeypatch.setattr(installation, "_read_control_file", read)
    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    authority = publish_generation_control(installed)
    assert events.index("pointer-read") < events.index(carrier)
    view = InstalledAuthorizationView.model_validate_json(carrier.read_bytes())
    assert all(
        getattr(view, item) == getattr(projection.target.identity, item)
        for item in installation._TARGET_POINTER_FIELDS
    )
    assert view.installed_state_id == authority.pointer.installed_state_id == INSTALLED_STATE_ID
    assert [event for event in events if isinstance(event, Path)] == [carrier, pointer, marker]


@pytest.mark.parametrize("state", ["malformed", "noncanonical", "actor"])
def test_dormant_carrier_refusal_precedes_pointer_access(
    store: Path, tmp_path: Path, monkeypatch, state: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    carrier, pointer = _authorization_view(store), _control_files(store)[1]
    raw = _authorization_bytes(
        projection, **({"installed_for_user_id": INSTALLED_STATE_ID} if state == "actor" else {})
    )
    carrier.write_bytes(
        b"{"
        if state == "malformed"
        else json.dumps(json.loads(raw)).encode()
        if state == "noncanonical"
        else raw
    )
    pointer.write_bytes(b"pointer")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    before = _bytes(installed.root_path), carrier.read_bytes(), pointer.read_bytes()
    _forbid_pointer_access(monkeypatch, pointer)
    _forbid_publication_effects(monkeypatch)
    expected = ReplicaActorMismatchError if state == "actor" else GenerationControlCorruptError
    message = (
        "The installed replica belongs to another account."
        if state == "actor"
        else "The installed authorization view is invalid."
    )
    _assert_control_error(
        expected, expected.code, message, lambda: publish_generation_control(installed)
    )
    assert (_bytes(installed.root_path), carrier.read_bytes(), pointer.read_bytes()) == before
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize(
    ("state", "expected", "message"),
    [
        (
            "carrier-missing",
            RefreshRequiredError,
            "The installed replica requires a fresh authorization view.",
        ),
        (
            "carrier-malformed",
            GenerationControlCorruptError,
            "The installed authorization view is invalid.",
        ),
        (
            "carrier-noncanonical",
            GenerationControlCorruptError,
            "The installed authorization view is invalid.",
        ),
        (
            "carrier-actor",
            ReplicaActorMismatchError,
            "The installed replica belongs to another account.",
        ),
        (
            "carrier-digest",
            RefreshRequiredError,
            "The installed replica requires a fresh authorization view.",
        ),
        (
            "pointer-missing",
            RefreshRequiredError,
            "The active account has no installed generation pointer.",
        ),
        (
            "pointer-malformed",
            GenerationControlCorruptError,
            "The installed generation pointer is invalid.",
        ),
        (
            "pointer-noncanonical",
            GenerationControlCorruptError,
            "The installed generation pointer is invalid.",
        ),
        (
            "pointer-actor",
            ReplicaActorMismatchError,
            "The installed replica belongs to another account.",
        ),
        (
            "pointer-state",
            RefreshRequiredError,
            "The installed replica requires a fresh authorization view.",
        ),
    ],
)
def test_active_pair_failures_never_repair(
    store: Path, monkeypatch, state: str, expected, message: str
) -> None:
    installed = install_generation_root(_projection())
    publish_generation_control(installed)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    path = carrier if state.startswith("carrier") else pointer
    if state in {"carrier-missing", "pointer-missing"}:
        path.unlink()
    elif state.endswith("malformed"):
        path.write_bytes(b"{")
    else:
        body = json.loads(path.read_bytes())
        field, value = (
            ("installed_for_user_id", INSTALLED_STATE_ID)
            if state.endswith("actor")
            else ("installed_state_id", INSTALLED_STATE_ID)
            if state == "pointer-state"
            else ("manifest_digest", "b" * 64)
        )
        body[field] = value
        path.write_bytes(
            json.dumps(
                body,
                sort_keys=not state.endswith("noncanonical"),
                separators=(",", ":") if not state.endswith("noncanonical") else None,
            ).encode()
        )
    before = tuple(p.read_bytes() if p.exists() else None for p in (marker, carrier, pointer))
    if state.startswith("carrier"):
        _forbid_pointer_access(monkeypatch, pointer)
    _forbid_publication_effects(monkeypatch)
    _assert_control_error(
        expected, expected.code, message, lambda: publish_generation_control(installed)
    )
    assert (
        tuple(p.read_bytes() if p.exists() else None for p in (marker, carrier, pointer)) == before
    )


@pytest.mark.parametrize(("switch", "residual"), [(1, 0), (2, 0), (3, 1), (4, 2), (5, 3)])
@pytest.mark.parametrize("failure", [None, "missing", "invalid", "unsafe", "ambiguous"])
def test_actor_switch_stops_at_each_publication_boundary(
    store: Path, monkeypatch, switch: int, residual: int, failure: str | None
) -> None:
    installed = install_generation_root(_projection())
    calls = 0

    def actor() -> str:
        nonlocal calls
        calls += 1
        if calls == switch:
            if failure is not None:
                raise ActiveUserReadError(failure)
            return INSTALLED_STATE_ID
        return USER_ID

    monkeypatch.setattr(installation, "read_active_user_id", actor)
    monkeypatch.setattr(installation, "generate_ulid", lambda: OTHER_STATE_ID)
    _assert_control_error(
        ReplicaActorMismatchError,
        "replica_actor_mismatch",
        "The installed replica does not match an active account."
        if failure is not None
        else "The installed replica belongs to another account.",
        lambda: publish_generation_control(installed),
    )
    marker, pointer = _control_files(store)
    paths = (_authorization_view(store), pointer, marker)
    assert sum(path.exists() for path in paths) == residual


@pytest.mark.parametrize(
    ("timing", "audit_call", "active", "residual"),
    [
        ("lock", 0, False, 0),
        ("carrier", 0, False, 0),
        ("root", 1, False, 0),
        ("final", 3, False, 3),
        ("active", 1, True, 3),
    ],
)
def test_actor_switch_during_each_verification_phase(
    store: Path, monkeypatch, timing: str, audit_call: int, active: bool, residual: int
) -> None:
    installed = install_generation_root(_projection())
    if active:
        publish_generation_control(installed)
    switched = False
    audits = 0
    real_audit = installation._audit_installed_root
    real_carrier_read = installation._read_actor_authorization_view
    real_lock_validation = installation._validate_lock_path

    def switch() -> None:
        nonlocal switched
        switched = True

    def audit(*args):
        nonlocal audits
        result = real_audit(*args)
        audits += 1
        if audits == audit_call:
            switch()
        return result

    def carrier_read(*args):
        result = real_carrier_read(*args)
        switch()
        return result

    def validate_lock(*args, **kwargs):
        result = real_lock_validation(*args, **kwargs)
        switch()
        return result

    if timing == "lock":
        monkeypatch.setattr(installation, "_validate_lock_path", validate_lock)
    elif timing == "carrier":
        monkeypatch.setattr(installation, "_read_actor_authorization_view", carrier_read)
    else:
        monkeypatch.setattr(installation, "_audit_installed_root", audit)
    monkeypatch.setattr(
        installation, "read_active_user_id", lambda: INSTALLED_STATE_ID if switched else USER_ID
    )
    _assert_control_error(
        ReplicaActorMismatchError,
        "replica_actor_mismatch",
        "The installed replica belongs to another account.",
        lambda: publish_generation_control(installed),
    )
    marker, pointer = _control_files(store)
    assert sum(path.exists() for path in (_authorization_view(store), pointer, marker)) == residual


@pytest.mark.parametrize("boundary", ["carrier", "pointer"])
@pytest.mark.parametrize(
    "race",
    ["replacement", "deletion", "hard-link", "partial-read", "size-change", "identity-change"],
)
def test_publication_reread_races_fail_without_a_later_mutation(
    store: Path, monkeypatch, boundary: str, race: str
) -> None:
    projection = _projection()
    installed = install_generation_root(projection)
    marker, pointer = _control_files(store)
    carrier = _authorization_view(store)
    target = carrier if boundary == "carrier" else pointer
    before = _bytes(installed.root_path)
    message = f"Replica control file changed during {'open' if race == 'replacement' else 'read'}."
    real_write = installation.atomic_write_bytes
    real_open, real_os_read, real_lstat = os.open, os.read, os.lstat
    writes = []
    target_fd = None
    read_done = fired = False
    raced = target.with_suffix(".race")

    def open_file(path, *args, **kwargs):
        nonlocal target_fd, fired
        if Path(path) == target and target in writes:
            if race == "replacement":
                raced.write_bytes(target.read_bytes())
                os.replace(raced, target)
                fired = True
            target_fd = real_open(path, *args, **kwargs)
            return target_fd
        return real_open(path, *args, **kwargs)

    def read_file(descriptor, length):
        nonlocal read_done, fired
        content = real_os_read(descriptor, length)
        if descriptor == target_fd:
            if race == "partial-read":
                fired = True
                return content[:-1]
            read_done = True
        return content

    def lstat(path, *args, **kwargs):
        nonlocal fired
        metadata = real_lstat(path, *args, **kwargs)
        field = {"hard-link": 3, "size-change": 6, "identity-change": 1}.get(race)
        if read_done and Path(path) == target and not fired:
            fired = True
            if race == "deletion":
                raise FileNotFoundError(path)
            values, attributes = metadata.__reduce__()[1]
            values = list(values)
            values[field] += 1
            return os.stat_result(values, attributes)
        return metadata

    def write(path, content):
        real_write(path, content)
        writes.append(path)

    monkeypatch.setattr(installation.os, "open", open_file)
    monkeypatch.setattr(installation.os, "read", read_file)
    monkeypatch.setattr(installation.os, "lstat", lstat)
    monkeypatch.setattr(installation, "atomic_write_bytes", write)
    monkeypatch.setattr(installation, "generate_ulid", lambda: INSTALLED_STATE_ID)
    _assert_control_error(
        ReplicaControlReadError,
        "generation_control_unavailable",
        message,
        lambda: publish_generation_control(installed),
    )
    assert _bytes(installed.root_path) == before
    assert fired is True
    assert writes == ([carrier] if boundary == "carrier" else [carrier, pointer])
    assert not marker.exists()
