"""Sync state tracking — manages .sync-state.json per project."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("nauro.sync")

SYNC_STATE_FILE = ".sync-state.json"


@dataclass
class FileState:
    local_sha256: str = ""
    remote_etag: str = ""
    last_sync: str = ""


@dataclass
class SyncState:
    files: dict[str, FileState] = field(default_factory=dict)
    last_full_sync: str = ""


def load_state(project_path: Path) -> SyncState:
    """Load .sync-state.json from project directory."""
    state_file = project_path / SYNC_STATE_FILE
    if not state_file.exists():
        return SyncState()
    try:
        data = json.loads(state_file.read_text())
        state = SyncState(last_full_sync=data.get("last_full_sync", ""))
        for rel_path, fdata in data.get("files", {}).items():
            state.files[rel_path] = FileState(
                local_sha256=fdata.get("local_sha256", ""),
                remote_etag=fdata.get("remote_etag", ""),
                last_sync=fdata.get("last_sync", ""),
            )
        return state
    except (json.JSONDecodeError, KeyError):
        logger.warning("Corrupt .sync-state.json, starting fresh")
        return SyncState()


def save_state(project_path: Path, state: SyncState) -> None:
    """Write .sync-state.json to project directory."""
    data = {
        "files": {
            rel_path: {
                "local_sha256": fs.local_sha256,
                "remote_etag": fs.remote_etag,
                "last_sync": fs.last_sync,
            }
            for rel_path, fs in state.files.items()
        },
        "last_full_sync": state.last_full_sync,
    }
    state_file = project_path / SYNC_STATE_FILE
    state_file.write_text(json.dumps(data, indent=2) + "\n")


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()


def file_changed_locally(project_path: Path, relative_path: str, state: SyncState) -> bool:
    """Check if local file has changed since last sync."""
    local_file = project_path / relative_path
    if not local_file.exists():
        return relative_path in state.files
    current_sha = compute_sha256(local_file)
    fs = state.files.get(relative_path)
    if fs is None:
        return True
    return current_sha != fs.local_sha256


def file_changed_remotely(remote_etag: str, relative_path: str, state: SyncState) -> bool:
    """Check if remote file has changed since last sync."""
    fs = state.files.get(relative_path)
    if fs is None:
        return True
    return remote_etag != fs.remote_etag


def update_file_state(
    state: SyncState, relative_path: str, local_sha256: str, remote_etag: str
) -> None:
    """Update the sync state entry for a file."""
    now = datetime.now(UTC).isoformat()
    state.files[relative_path] = FileState(
        local_sha256=local_sha256,
        remote_etag=remote_etag,
        last_sync=now,
    )
