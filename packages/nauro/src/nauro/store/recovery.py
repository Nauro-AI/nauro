"""Validated local binding and atomic cloud-store restoration."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import httpx
from nauro_core.doctor import diagnose_store
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from nauro.cli.commands.auth import AuthRefreshError
from nauro.constants import DECISIONS_DIR, PROJECT_MD
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.registry import bind_project_store_v2
from nauro.store.repo_config import load_repo_config
from nauro.store.resolution import RepoResolution
from nauro.sync.cloud_projects import CloudProjectError, list_projects
from nauro.sync.remote import (
    PresignError,
    fetch_manifest,
    fetch_via_presigned_url,
    request_presigned_urls,
)


class RecoveryError(RuntimeError):
    """A recovery action cannot complete without risking local state."""


class CloudManifestEntry(BaseModel):
    """Validated cloud manifest entry used during staged restoration."""

    model_config = ConfigDict(extra="ignore")

    path: StrictStr
    etag: StrictStr
    size: Annotated[StrictInt, Field(ge=0)] | None = None
    sha256: StrictStr | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, raw_path: str) -> str:
        if not raw_path or "\\" in raw_path:
            raise ValueError("manifest path must be a nonempty POSIX path")
        path = Path(raw_path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != raw_path:
            raise ValueError("manifest path must stay within the project store")
        return raw_path


class CloudPresignResult(BaseModel):
    """Validated download URL returned by the cloud presign endpoint."""

    model_config = ConfigDict(extra="ignore")

    verb: Literal["GET"]
    path: StrictStr
    url: StrictStr

    @field_validator("path", "url")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("presign fields must be nonempty")
        return value


_MANIFEST_ADAPTER = TypeAdapter(list[CloudManifestEntry])
_PRESIGN_ADAPTER = TypeAdapter(list[CloudPresignResult])


def _parse_manifest(raw_manifest: object) -> list[CloudManifestEntry]:
    try:
        return _MANIFEST_ADAPTER.validate_python(raw_manifest)
    except ValidationError as exc:
        raise RecoveryError("Invalid cloud manifest payload.") from exc


def _parse_presign_results(raw_results: object) -> list[CloudPresignResult]:
    try:
        return _PRESIGN_ADAPTER.validate_python(raw_results)
    except ValidationError as exc:
        raise RecoveryError("Invalid cloud restore presign payload.") from exc


def require_cloud_membership(project_id: str) -> str:
    """Return the cloud project name after verifying current membership."""
    try:
        projects = list_projects()
    except CloudProjectError as exc:
        raise RecoveryError(str(exc)) from exc
    match = next((project for project in projects if project["project_id"] == project_id), None)
    if match is None:
        raise RecoveryError(f"Project id {project_id!r} not found among your cloud projects.")
    name = match["name"]
    if not name:
        raise RecoveryError(f"Cloud project {project_id!r} has no valid name.")
    return name


def bind_local_store(repo_path: Path, store_path: Path) -> RepoResolution:
    """Bind an existing store using identity from the repository config."""
    config = load_repo_config(repo_path)
    bound = bind_project_store_v2(
        project_id=config["id"],
        name=config["name"],
        mode=config["mode"],
        repo_path=repo_path,
        store_path=store_path,
        server_url=config.get("server_url"),
    )
    return RepoResolution(bound, config["id"], config["name"])


def _validate_restored_store(store_path: Path) -> None:
    project_file = store_path / PROJECT_MD
    decisions_dir = store_path / DECISIONS_DIR
    if (
        not project_file.is_file()
        or project_file.is_symlink()
        or not decisions_dir.is_dir()
        or decisions_dir.is_symlink()
    ):
        raise RecoveryError("Cloud record is not a complete Nauro store.")
    diagnosis = diagnose_store(FilesystemStore(store_path))
    if not diagnosis.is_clean:
        raise RecoveryError("Cloud record failed decision-store integrity validation.")


def _destination_is_available(destination: Path) -> bool:
    if not destination.exists():
        return True
    if not destination.is_dir():
        return False
    return next(destination.iterdir(), None) is None


def _etag_md5(raw_etag: str, relative: str) -> str:
    value = raw_etag.strip('"').lower()
    if len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
        raise RecoveryError(f"Cloud manifest has an unusable content hash for {relative}.")
    return value


def restore_cloud_store(project_id: str, destination: Path) -> Path:
    """Restore a complete remote record into an absent or empty destination."""
    if not destination.is_absolute() or destination.resolve(strict=False) != destination:
        raise RecoveryError(f"Restore destination must be canonical and absolute: {destination}.")
    if not _destination_is_available(destination):
        raise RecoveryError(f"Refusing to overwrite nonempty destination: {destination}.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{project_id}.restore-", dir=destination.parent))
    installed = False
    try:
        try:
            manifest = _parse_manifest(fetch_manifest(project_id))
        except (AuthRefreshError, PresignError, httpx.HTTPError) as exc:
            raise RecoveryError(f"Cloud manifest fetch failed: {exc}") from exc
        if not manifest:
            raise RecoveryError("Cloud project has no stored record to restore.")

        entries: dict[str, CloudManifestEntry] = {}
        for entry in manifest:
            relative = entry.path
            if relative in entries:
                raise RecoveryError(f"Duplicate cloud manifest path: {relative!r}.")
            entries[relative] = entry

        operations = [{"verb": "GET", "path": path} for path in entries]
        try:
            urls = _parse_presign_results(request_presigned_urls(project_id, operations))
        except (AuthRefreshError, PresignError, httpx.HTTPError) as exc:
            raise RecoveryError(f"Cloud restore presign failed: {exc}") from exc
        url_by_path: dict[str, str] = {}
        for item in urls:
            if item.path in url_by_path:
                raise RecoveryError(f"Duplicate cloud restore URL for {item.path!r}.")
            url_by_path[item.path] = item.url
        if set(url_by_path) != set(entries):
            raise RecoveryError("Cloud restore did not receive one download URL per manifest file.")

        for relative, entry in entries.items():
            try:
                content = fetch_via_presigned_url(url_by_path[relative])
            except (PresignError, httpx.HTTPError) as exc:
                raise RecoveryError(f"Cloud download failed for {relative}: {exc}") from exc
            expected_size = entry.size
            if expected_size is not None and len(content) != expected_size:
                raise RecoveryError(f"Cloud size validation failed for {relative}.")
            if hashlib.md5(content).hexdigest() != _etag_md5(entry.etag, relative):
                raise RecoveryError(f"Cloud content-hash validation failed for {relative}.")
            digest = hashlib.sha256(content).hexdigest()
            expected_hash = entry.sha256
            if expected_hash is not None and digest != expected_hash:
                raise RecoveryError(f"Cloud hash validation failed for {relative}.")
            target = staging / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except OSError as exc:
                raise RecoveryError(f"Could not stage cloud file {relative}: {exc}") from exc

        _validate_restored_store(staging)
        os.replace(staging, destination)
        installed = True
        return destination
    finally:
        if not installed and staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "RecoveryError",
    "bind_local_store",
    "require_cloud_membership",
    "restore_cloud_store",
]
