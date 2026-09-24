"""Retained identity for one explicit initial attachment."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nauro.store._atomic import is_tmp_sibling
from nauro.store.generation_authority import (
    RefreshRequiredError,
    _parse_authorization_view,
    _parse_pointer,
)
from nauro.store.generation_installation import (
    _layout,
    _read_expected,
    _validate_carrier,
    _validate_control_pair,
)
from nauro.store.generation_projection import (
    GenerationProjectionIdentity,
    VerifiedGenerationProjection,
)
from nauro.store.generation_refresh_intent import decode_intent
from nauro.store.generation_refresh_io import (
    RefreshPaths,
    durable_replace,
    read_evidence,
    refresh_paths,
    sync_file,
    sync_parents,
)
from nauro.store.home import nauro_home
from nauro.store.replica_control import _validate_managed_path
from nauro.sync.generation_credentials import GenerationConnection


class AttachmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    repo: str
    store: str
    connection: str
    projection: GenerationProjectionIdentity

    def require_binding(self, repo: Path, store: Path, connection: GenerationConnection) -> None:
        if (self.repo, self.store, self.connection) != (
            str(repo.resolve()),
            str(store),
            connection.binding(),
        ):
            raise RefreshRequiredError("Retained attachment belongs to another connection or path.")


def record_path(project: str) -> Path:
    return nauro_home() / f"generation-attachment-{project}.json"


def read_record(project: str) -> AttachmentRecord | None:
    home = nauro_home()
    raw = read_evidence(RefreshPaths(home, home), record_path(project))
    if raw is None:
        return None
    try:
        record = AttachmentRecord.model_validate_json(raw)
    except ValueError:
        raise RefreshRequiredError("Retained attachment evidence is invalid.") from None
    if record.projection.project_id != project or record.model_dump_json().encode() != raw:
        raise RefreshRequiredError("Retained attachment evidence is not exact.")
    return record


def save_record(record: AttachmentRecord) -> None:
    project = record.projection.project_id
    prior = read_record(project)
    if prior is not None and prior != record:
        raise RefreshRequiredError("Retained attachment cannot be replaced.")
    home = nauro_home()
    paths = RefreshPaths(home, home)
    path = record_path(project)
    if prior is None:
        durable_replace(paths, path, record.model_dump_json().encode())
    sync_file(paths, path)
    sync_parents(paths, home)


def _staging_file(store: Path, path: Path, expected: bytes, *, partial: bool) -> None:
    _validate_managed_path(store, path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > len(expected):
        raise RefreshRequiredError("Unsafe interrupted staging is preserved intact.")
    raw = _read_expected(path, before.st_size, (before.st_dev, before.st_ino), "staging")
    _validate_managed_path(store, path)
    after = path.lstat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
    if (
        tuple(getattr(before, name) for name in fields)
        != tuple(getattr(after, name) for name in fields)
        or len(raw) != before.st_size
        or raw != expected[: len(raw)]
        or (not partial and len(raw) != len(expected))
    ):
        raise RefreshRequiredError("Changed interrupted staging is preserved intact.")


def _staging_paths(projection: VerifiedGenerationProjection) -> tuple[set[Path], set[Path]]:
    store = projection.target.binding.store_path
    layout = _layout(store, projection.target)
    _validate_managed_path(store, layout.staging_dir)
    if not layout.staging_dir.exists():
        return set(), set()
    contents = {
        "manifest.json": projection.manifest_json,
        **{"store/" + a.path: a.content for a in projection.artifacts},
    }
    expected_dirs = {"store"}
    for relative in contents:
        expected_dirs.update(p.as_posix() for p in Path(relative).parents if p != Path("."))
    files, directories = set(), set()
    for staged in layout.staging_dir.iterdir():
        _validate_managed_path(store, staged)
        if (
            not staged.is_dir()
            or re.fullmatch(re.escape(layout.root_key) + "-[0-9a-f]{16}", staged.name) is None
        ):
            raise RefreshRequiredError("Unrecognized attachment evidence is preserved intact.")
        directories.add(staged)
        containers = [staged]
        for directory in containers:
            for path in directory.iterdir():
                _validate_managed_path(store, path)
                relative = path.relative_to(staged).as_posix()
                if path.is_dir() and relative in expected_dirs:
                    directories.add(path)
                    containers.append(path)
                    continue
                partial = is_tmp_sibling(path.name)
                target = path.with_name(path.name[1:].rsplit(".", 2)[0]) if partial else path
                expected = contents.get(target.relative_to(staged).as_posix())
                if expected is None:
                    raise RefreshRequiredError(
                        "Unrecognized attachment evidence is preserved intact."
                    )
                _staging_file(store, path, expected, partial=partial)
                files.add(path)
    return files, directories


def validate_retained_projection(projection: VerifiedGenerationProjection) -> bool:
    target = projection.target
    store = target.binding.store_path
    if not store.exists():
        return False
    paths = refresh_paths(target.binding, target.identity.installed_for_user_id)
    layout = _layout(store, target)
    files = {
        store / ".replica-control.lock",
        paths.marker,
        paths.pointer,
        paths.carrier,
        paths.intent,
        layout.root_path / "manifest.json",
        *(layout.root_path / "store" / item.path for item in projection.artifacts),
    }
    directories = {layout.staging_dir, layout.root_path / "store"}
    for file in files:
        directories.update(parent for parent in file.parents if store in parent.parents)
    staging_files, staging_directories = _staging_paths(projection)
    files.update(staging_files)
    directories.update(staging_directories)
    for path in store.rglob("*"):
        _validate_managed_path(store, path)
        if not ((path.is_dir() and path in directories) or (path.is_file() and path in files)):
            raise RefreshRequiredError("Unrecognized attachment evidence is preserved intact.")
    intent_raw = read_evidence(paths, paths.intent)
    if intent_raw is not None:
        intent = decode_intent(intent_raw)
        for raw in (intent.base_pointer_json, intent.target_pointer_json):
            pointer = _parse_pointer(raw)
            if any(
                getattr(pointer, name) != getattr(target.identity, name)
                for name in GenerationProjectionIdentity.model_fields
            ):
                raise RefreshRequiredError("Retained refresh differs from the initial attachment.")
        return True
    marker = read_evidence(paths, paths.marker)
    carrier_raw = read_evidence(paths, paths.carrier)
    pointer_raw = read_evidence(paths, paths.pointer)
    actor = target.identity.installed_for_user_id
    carrier = _parse_authorization_view(carrier_raw) if carrier_raw is not None else None
    if carrier is not None and not _validate_carrier(target, actor, marker, carrier):
        raise RefreshRequiredError("Retained attachment authorization differs.")
    if pointer_raw is not None and (
        carrier is None
        or not _validate_control_pair(target, actor, marker, carrier, _parse_pointer(pointer_raw))
    ):
        raise RefreshRequiredError("Retained attachment pointer differs.")
    if marker is not None and (carrier is None or pointer_raw is None):
        raise RefreshRequiredError("Retained attachment controls are incomplete.")
    return False
