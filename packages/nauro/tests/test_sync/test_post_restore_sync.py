"""What the first sync after a cloud restore does.

A restore downloads and verifies every file the server holds, then installs
them. Nothing but this seam records that fact, because ``.sync-state.json`` is
never synced and so never arrives from the remote either. Without it the next
pull reads "no entry" as "changed on both sides" for the whole store, and these
tests hold that shut end to end: through ``restore_cloud_store`` itself, through
``nauro reconnect``, and across a restore that had to resume.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from nauro.cli.main import app
from nauro.constants import REPO_CONFIG_MODE_CLOUD
from nauro.store import recovery
from nauro.store.recovery import RecoveryError, restore_cloud_store
from nauro.store.registry import get_store_path_v2, register_project_v2
from nauro.store.repo_config import save_repo_config
from nauro.sync.merge import CONFLICT_BACKUP_DIR, should_skip
from nauro.sync.pull import PullReport, run_pull
from nauro.sync.quarantine import list_quarantine_backups
from nauro.sync.remote import PresignTransferError
from nauro.sync.state import SYNC_STATE_FILE, load_state
from nauro.templates.scaffolds import scaffold_project_store
from tests.test_sync.conftest import (
    CLOUD_PID,
    _manifest,
    _presign,
    _RecordingReporter,
    _seed_token,
    decision_bytes,
)

runner = CliRunner()

_DECISION = "decisions/042-a-ruling.md"


def _scaffolded_bytes(root: Path) -> dict[str, bytes]:
    """Every file of a freshly scaffolded store, keyed store-relative."""
    scaffold_project_store("nauro", root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class _Remote:
    """One remote record behind both the restore's calls and the pull's.

    The restore and the pull reach the server through different seams, so the
    fake serves both from one dict of bytes. That is what lets a test change a
    file on the server between the two and see what the pull makes of it.
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.fetched: list[str] = []
        self.faults: dict[str, Exception] = {}

    # --- the restore's three calls ---

    def rows(self, _project_id: str | None = None) -> list[dict]:
        return [
            {
                "path": path,
                "etag": f'"{hashlib.md5(content).hexdigest()}"',
                "size": len(content),
                "last_modified": "2026-08-12T00:00:00Z",
            }
            for path, content in sorted(self.files.items())
        ]

    def _presigned(self, _project_id: str, operations: list[dict[str, str]]) -> list[dict]:
        return [
            {"verb": "GET", "path": op["path"], "url": f"memory://{op['path']}"}
            for op in operations
        ]

    def _fetch(self, url: str) -> bytes:
        path = url.removeprefix("memory://")
        self.fetched.append(path)
        fault = self.faults.get(path)
        if fault is not None:
            raise fault
        return self.files[path]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(recovery, "fetch_manifest", self.rows)
        monkeypatch.setattr(recovery, "request_presigned_urls", self._presigned)
        monkeypatch.setattr(recovery, "fetch_via_presigned_url", self._fetch)

    # --- the pull's two calls ---

    @contextmanager
    def serving_pull(self) -> Iterator[None]:
        def get(url: str, **_kwargs) -> httpx.Response:
            if "/sync/manifest" in url:
                return _manifest(self.rows())
            path = url.split("/GET/", 1)[1]
            self.fetched.append(path)
            return httpx.Response(200, content=self.files[path])

        def post(_url: str, **kwargs) -> httpx.Response:
            return _presign(kwargs["json"]["operations"])

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=get),
            patch("nauro.sync.remote.httpx.post", side_effect=post),
        ):
            yield


@pytest.fixture()
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Remote:
    files = _scaffolded_bytes(tmp_path / "source")
    files[_DECISION] = decision_bytes(42, "A ruling", "Chose A.")
    served = _Remote(files)
    served.install(monkeypatch)
    _seed_token()
    return served


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    """The registered cloud store path, with nothing on disk yet."""
    _project_id, store_path = register_project_v2(
        "nauro",
        [tmp_path / "repo"],
        mode=REPO_CONFIG_MODE_CLOUD,
        server_url="https://example.test",
        project_id=CLOUD_PID,
    )
    return store_path


def _pull(store_path: Path, remote: _Remote) -> tuple[PullReport, _RecordingReporter]:
    reporter = _RecordingReporter()
    remote.fetched.clear()
    with remote.serving_pull():
        return run_pull(CLOUD_PID, store_path, reporter), reporter


def _backups(store_path: Path) -> list[str]:
    return sorted(
        path.name for path in (store_path / CONFLICT_BACKUP_DIR).rglob("*") if path.is_file()
    )


class TestPullAfterRestore:
    def test_remote_decision_edit_lands_on_the_pull_after_a_restore(self, remote, store):
        """The worst shape this defect had, held shut end to end.

        An untracked decision reaches the collision gate, and a remote body that
        moved on is judged a two-sided conflict there: the stale local copy
        wins, the server's copy goes to the backup directory, and the pull then
        records the new ETag against the file it did not update, so no later
        pull ever fetches it again. A decision edited on another machine simply
        never arrived.
        """
        restore_cloud_store(CLOUD_PID, store)
        updated = decision_bytes(42, "A ruling", "Chose A. Rider: held under load.")
        remote.files[_DECISION] = updated

        report, _reporter = _pull(store, remote)

        assert (store / _DECISION).read_bytes() == updated
        assert _backups(store) == []
        assert list_quarantine_backups(store) == []
        assert report.merged == 1
        assert report.refused == 0

    def test_restore_then_pull_writes_nothing_and_fetches_nothing(self, remote, store):
        restore_cloud_store(CLOUD_PID, store)

        report, reporter = _pull(store, remote)

        assert remote.fetched == []
        assert _backups(store) == []
        assert report == PullReport()
        assert reporter.infos == ["No remote changes"]
        assert reporter.warns == []

    def test_reconnect_restore_then_pull_fetches_nothing(self, remote, tmp_path, monkeypatch):
        """The other command that restores, driven through its own surface.

        Registration is left to ``reconnect`` here: the command exists for a
        repo whose store this machine does not have, so a pre-registered store
        would send it down a different branch entirely.
        """
        repo = tmp_path / "unconnected-repo"
        repo.mkdir()
        save_repo_config(
            repo,
            {
                "mode": "cloud",
                "id": CLOUD_PID,
                "name": "nauro",
                "server_url": "https://example.test",
            },
        )
        monkeypatch.chdir(repo)

        with patch("nauro.cli.commands.reconnect.require_cloud_membership", return_value="nauro"):
            result = runner.invoke(app, ["reconnect"], input="restore\n")
        assert result.exit_code == 0, result.output
        store = get_store_path_v2(CLOUD_PID)

        report, _reporter = _pull(store, remote)

        assert remote.fetched == []
        assert _backups(store) == []
        assert report == PullReport()


class TestSeededState:
    def test_seeded_state_is_installed_and_never_pushed(self, remote, store):
        restore_cloud_store(CLOUD_PID, store)

        state = load_state(store)
        assert set(state.files) == set(remote.files)
        for rel, content in remote.files.items():
            entry = state.files[rel]
            assert entry.remote_etag == f'"{hashlib.md5(content).hexdigest()}"'
            assert entry.local_sha256 == hashlib.sha256(content).hexdigest()
            assert entry.last_sync
        # The store is level with the server at this instant, which is the one
        # thing the stamp claims.
        assert state.last_full_sync
        # The file is in the store, and the sync layer refuses to carry it.
        assert (store / SYNC_STATE_FILE).is_file()
        assert should_skip(SYNC_STATE_FILE)

    def test_a_restore_that_fails_before_install_leaves_no_state(self, remote, store):
        remote.faults[_DECISION] = PresignTransferError("Presigned GET", status=404, detail="gone")

        with pytest.raises(RecoveryError):
            restore_cloud_store(CLOUD_PID, store)

        assert not (store / SYNC_STATE_FILE).exists()
        staging = store.parent / f".{CLOUD_PID}.restore"
        assert staging.is_dir()
        assert not (staging / SYNC_STATE_FILE).exists()

    def test_a_resumed_restore_seeds_every_installed_file(self, remote, store):
        remote.faults[_DECISION] = PresignTransferError("Presigned GET", status=404, detail="gone")
        with pytest.raises(RecoveryError):
            restore_cloud_store(CLOUD_PID, store)
        staged_first = set(remote.fetched)
        assert len(staged_first) > 1

        remote.faults.clear()
        remote.fetched.clear()
        restore_cloud_store(CLOUD_PID, store)

        # The second run downloaded only what the first one never staged, so a
        # seed built from that run alone would track almost nothing.
        assert set(remote.fetched) < set(remote.files)
        assert set(load_state(store).files) == set(remote.files)
