"""Retained identity for one explicit initial attachment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nauro.store.generation_authority import (
    RefreshRequiredError,
    _parse_authorization_view,
    _parse_pointer,
)
from nauro.store.generation_installation import _layout, _validate_carrier, _validate_control_pair
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
