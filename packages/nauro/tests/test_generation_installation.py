from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from inspect import signature
from pathlib import Path

import pytest

from nauro.store import generation_installation as installation
from nauro.store import replica_control
from nauro.store.generation_authority import FlatProjectAuthority, GenerationAuthorityError
from nauro.store.generation_installation import (
    GenerationInstallError,
    GenerationRootAudit,
    GenerationRootDivergedError,
    StagedGenerationRoot,
    audit_generation_tree,
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
    ReplicaControlReadError,
    _is_link_or_reparse,
    locked_replica_control_snapshot,
)
from nauro.store.resolution import ResolvedProjectBinding
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_sync.conftest import entry_names

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filename semantics")
LINK_KINDS = ["symlink", "junction"]
PROJECT_ID = "01KQ6AZGNA0B3QBF67NBXP3S45"
GENERATION_ID = "01K11111111111111111111111"
USER_ID = "01K33333333333333333333333"
SCOPE_ID = "a" * 64
COMMITTED_AT = "2999-12-31T23:59:59.999999Z"
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


def _manifest(artifacts: dict[str, bytes], scope_id: str = SCOPE_ID) -> bytes:
    body = {
        "project_id": PROJECT_ID,
        "store_format_version": 1,
        "generation_id": GENERATION_ID,
        "projection_class": "contributor_plus",
        "projection_scope_id": scope_id,
        "artifacts": {path: hashlib.sha256(body).hexdigest() for path, body in artifacts.items()},
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _projection(artifacts: dict[str, bytes] | None = None) -> VerifiedGenerationProjection:
    artifacts = ARTIFACTS if artifacts is None else artifacts
    manifest = _manifest(artifacts)
    binding = ResolvedProjectBinding(
        get_store_path_v2(PROJECT_ID), PROJECT_ID, "Nauro", "cloud", "https://mcp.nauro.ai"
    )
    identity = GenerationProjectionIdentity(
        project_id=PROJECT_ID,
        store_format_version=1,
        generation_id=GENERATION_ID,
        manifest_digest=hashlib.sha256(manifest).hexdigest(),
        committed_at=COMMITTED_AT,
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


@pytest.mark.parametrize(("mutation", "message"), AUDIT_ROWS)
def test_audit_refuses_each_mutation(store, tmp_path, monkeypatch, mutation: str, message: str):
    if mutation in LINK_KINDS:
        _require_link_kind(mutation)
    projection = _projection()
    staging = stage_generation_root(projection).staging_path
    if mutation == "extra-file":
        (staging / "store" / "extra.md").write_bytes(b"extra\n")
    elif mutation == "extra-dir":
        (staging / "store" / "extra").mkdir()
    elif mutation == "missing":
        (staging / "store" / "context" / "brief.md").unlink()
    elif mutation == "byte":
        (staging / "store" / "decisions" / "001-x.md").write_bytes(b"# 002\n")
    elif mutation == "symlink":
        victim = staging / "store" / "context" / "brief.md"
        victim.unlink()
        victim.symlink_to(staging / "store" / "decisions" / "001-x.md")
    elif mutation == "junction":
        shutil.rmtree(staging / "store" / "context")
        _link(staging / "store" / "context", staging / "store" / "decisions", "junction")
    elif mutation == "hard-link":
        os.link(staging / "store" / "decisions" / "001-x.md", tmp_path / "alias.md")
    elif mutation == "fifo":
        (staging / "manifest.json").unlink()
        os.mkfifo(staging / "manifest.json")
        real_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == staging / "manifest.json":
                pytest.fail("opened the fifo")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(installation.os, "open", guarded_open)
    elif mutation == "tmp":
        (staging / "store" / ".project.md.0123456789abcdef.tmp").write_bytes(b"")
    elif mutation == "manifest-byte":
        raw = bytearray(projection.manifest_json)
        raw[-1] ^= 1
        (staging / "manifest.json").write_bytes(bytes(raw))
    else:
        (staging / "manifest.json").write_bytes(_manifest(ARTIFACTS, "b" * 64))
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
    staged = stage_generation_root(_projection())
    with pytest.raises(FrozenInstanceError):
        staged.root_key = "x"
