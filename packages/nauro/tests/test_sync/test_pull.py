"""Tests for the shared pull core (``nauro.sync.pull``).

``run_pull`` is the single pull-and-merge implementation behind both
``nauro sync`` and the SessionStart hook. The two callers differ only in
their :class:`~nauro.sync.pull.Reporter`: the CLI echoes to the terminal,
the hook logs quietly. These tests drive the core directly with a
recording stub.

The decision-number collision matrix is exercised end to end here; the
classifier and its crash windows are unit-tested in ``test_collisions.py``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from nauro_core.operations.propose_decision import _next_decision_num

from nauro.store.filesystem_store import FilesystemStore
from nauro.sync import pull as pull_module
from nauro.sync.corpus import DecisionCorpus
from nauro.sync.lock import SyncLockTimeoutError, decision_lock
from nauro.sync.merge import SPOOL_DIR_PREFIX, should_skip
from nauro.sync.pull import _Transfer, run_pull
from nauro.sync.quarantine import (
    list_quarantine_backups,
    save_quarantine_backup,
    unresolved_quarantines,
)
from nauro.sync.state import (
    FileState,
    SyncState,
    compute_sha256,
    load_state,
    save_state,
)
from tests.conftest import seed_auth_config
from tests.test_sync.conftest import CLOUD_PID, _scaffolded_cloud_project


def _ok(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _seed_token() -> None:
    seed_auth_config(variant="sync")


def _manifest(files, next_cursor=None) -> httpx.Response:
    return _ok(200, {"files": files, "next_cursor": next_cursor})


def _presign(ops) -> httpx.Response:
    return _ok(
        200,
        {
            "urls": [
                {
                    "verb": op["verb"],
                    "path": op["path"],
                    "url": f"https://s3.example/{op['verb']}/{op['path']}",
                    "expires_at": "2026-05-16T13:00:00Z",
                }
                for op in ops
            ]
        },
    )


# A canonical decision carrying the base_commit provenance stamp as its
# trailing frontmatter key. Sync moves raw bytes, so stamped files must
# round-trip byte-identically like any other decision.
_STAMPED_SHA = "a1b2c3d4" * 5
_STAMPED_DECISION = (
    "---\n"
    "date: 2026-08-05\n"
    "version: 1\n"
    "status: active\n"
    "confidence: high\n"
    "decision_type: null\n"
    "reversibility: null\n"
    "source: null\n"
    "files_affected: []\n"
    "supersedes: null\n"
    "superseded_by: null\n"
    f"base_commit: {_STAMPED_SHA}\n"
    "---\n\n"
    "# 099 — Remote stamped decision\n\n"
    "## Decision\n\nChose A.\n"
).encode()


class _RecordingReporter:
    """Records reported messages."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)


# --- run_pull happy path ---


class TestRunPullCleanPull:
    @pytest.fixture()
    def cloud_store(self, tmp_path):
        store = _scaffolded_cloud_project("pullcore", tmp_path)
        _seed_token()
        return store

    def test_clean_pull_writes_file_and_updates_state(self, cloud_store):
        rel = "decisions/099-remote.md"
        manifest = _manifest([{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"# 099\nfresh remote body\n")

        reporter = _RecordingReporter()
        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, reporter)

        assert merged == 1
        assert (cloud_store / rel).read_bytes() == b"# 099\nfresh remote body\n"
        state = load_state(cloud_store)
        assert state.files[rel].remote_etag == '"new"'
        assert reporter.infos == ["Merged 1 file(s) from remote"]
        assert reporter.warns == []

    def test_clean_pull_of_stamped_decision_is_byte_identical(self, cloud_store):
        rel = "decisions/099-remote-stamped.md"
        manifest = _manifest([{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=_STAMPED_DECISION)

        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, _RecordingReporter())

        assert merged == 1
        assert (cloud_store / rel).read_bytes() == _STAMPED_DECISION

    def test_append_only_conflict_invokes_resolve_and_writes_merge(self, cloud_store):
        # state_history.md is append-only with section-aware set-union merge.
        rel = "state_history.md"
        local = cloud_store / rel
        local.write_text("## History\n\nlocal entry\n")
        local_sha = compute_sha256(local)

        state = SyncState()
        state.files[rel] = FileState(
            local_sha256="old_sha",
            remote_etag='"old_etag"',
            last_sync="2026-05-16T00:00:00Z",
        )
        save_state(cloud_store, state)

        manifest = _manifest([{"path": rel, "etag": '"new_etag"', "size": 1, "last_modified": "x"}])
        presign = _presign([{"verb": "GET", "path": rel}])

        def fake_get(url, **kwargs):
            if "/sync/manifest" in url:
                return manifest
            return httpx.Response(200, content=b"## History\n\nremote entry\n")

        reporter = _RecordingReporter()
        with (
            patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, reporter)

        assert merged == 1
        merged_bytes = local.read_bytes()
        # Union of both sides — neither entry was dropped.
        assert b"local entry" in merged_bytes
        assert b"remote entry" in merged_bytes
        assert compute_sha256(local) != local_sha
        assert reporter.warns == []


# --- decision-number collisions: one classification per remote file ---


def decision_bytes(
    num: int,
    title: str,
    rationale: str = "Chose A.",
    *,
    status: str = "active",
    supersedes: str | None = None,
    superseded_by: str | None = None,
    separator: str = "—",
) -> bytes:
    """A canonical decision file body, parseable by ``parse_decision``."""
    return (
        "---\n"
        "date: 2026-08-10\n"
        "version: 1\n"
        f"status: {status}\n"
        "confidence: high\n"
        "decision_type: null\n"
        "reversibility: null\n"
        "source: null\n"
        "files_affected: []\n"
        f"supersedes: {repr(supersedes) if supersedes else 'null'}\n"
        f"superseded_by: {repr(superseded_by) if superseded_by else 'null'}\n"
        "---\n\n"
        f"# {num:03d} {separator} {title}\n\n"
        "## Decision\n\n"
        f"{rationale}\n"
    ).encode()


def write_local_decision(store, filename: str, content: bytes) -> object:
    decisions = store / "decisions"
    decisions.mkdir(exist_ok=True)
    path = decisions / filename
    path.write_bytes(content)
    return path


def pull(store, entries, *, reporter=None, etags=None):
    """Run one pull against a fake server holding exactly ``entries``.

    ``entries`` is an ordered list of ``(path, body)`` pairs so the tests can
    pin manifest order where order is the thing under test.
    """
    reporter = reporter or _RecordingReporter()
    etags = etags or {}
    bodies = dict(entries)
    manifest = _manifest(
        [
            {
                "path": rel,
                "etag": etags.get(rel, f'"{rel}-v1"'),
                "size": len(body),
                "last_modified": "x",
            }
            for rel, body in entries
        ]
    )
    presign = _presign([{"verb": "GET", "path": rel} for rel, _body in entries])

    def fake_get(url, **kwargs):
        if "/sync/manifest" in url:
            return manifest
        return httpx.Response(200, content=bodies[url.split("/GET/", 1)[1]])

    with (
        patch("nauro.sync.remote.httpx.get", side_effect=fake_get),
        patch("nauro.sync.remote.httpx.post", return_value=presign),
    ):
        merged = run_pull(CLOUD_PID, store, reporter)
    return merged, reporter


def entry_names(directory) -> set[str]:
    """Actual on-disk entry names.

    ``Path.exists()`` folds case on macOS and Windows, so an assertion about
    what a pull did or did not write has to read the directory itself.
    """
    return {entry.name for entry in directory.iterdir()} if directory.is_dir() else set()


def track(store, rel: str) -> None:
    """Record a sync-state entry for a local file, as a completed push would."""
    state = load_state(store)
    state.files[rel] = FileState(
        local_sha256=compute_sha256(store / rel),
        remote_etag='"pushed"',
        last_sync="2026-08-10T00:00:00Z",
    )
    save_state(store, state)


@pytest.fixture()
def collision_store(tmp_path):
    store = _scaffolded_cloud_project("collisioncore", tmp_path)
    _seed_token()
    return store


class TestQuarantine:
    """Published, referenced, or ambiguous colliders are never auto-resolved."""

    def test_tracked_collider_quarantines(self, collision_store):
        local = write_local_decision(
            collision_store, "003-local.md", decision_bytes(3, "Local decision")
        )
        original = local.read_bytes()
        track(collision_store, "decisions/003-local.md")

        remote = decision_bytes(3, "Remote decision", "Chose B.")
        merged, reporter = pull(collision_store, [("decisions/003-remote.md", remote)])

        assert merged == 0
        assert local.read_bytes() == original
        assert not (collision_store / "decisions/003-remote.md").exists()
        assert "decisions/003-remote.md" not in load_state(collision_store).files
        assert len(reporter.warns) == 1
        assert "already published" in reporter.warns[0]
        assert "Nauro app" in reporter.warns[0]

    def test_collider_in_manifest_quarantines(self, collision_store):
        """The push-crash shape: the PUT landed, the state save did not."""
        local = write_local_decision(
            collision_store, "003-local.md", decision_bytes(3, "Local decision")
        )
        original = local.read_bytes()

        merged, reporter = pull(
            collision_store,
            [
                ("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B.")),
                ("decisions/003-local.md", original),
            ],
        )

        assert local.read_bytes() == original
        assert not (collision_store / "decisions/003-remote.md").exists()
        assert merged == 0
        assert any("server manifest" in warning for warning in reporter.warns)
        # The same run adopts the local file the server already holds.
        assert "decisions/003-local.md" in load_state(collision_store).files

    def test_supersede_lineage_collider_quarantines(self, collision_store):
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))
        write_local_decision(
            collision_store,
            "004-child.md",
            decision_bytes(4, "Child decision", supersedes="3"),
        )

        merged, reporter = pull(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B."))],
        )

        assert merged == 0
        assert (collision_store / "decisions/003-local.md").exists()
        assert any("supersession" in warning for warning in reporter.warns)

    def test_question_resolution_reference_quarantines(self, collision_store):
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))
        (collision_store / "open-questions.md").write_text(
            "# Open Questions\n\n- [Resolved by D3 on 2026-08-01] [Q7] Which storage layout?\n"
        )

        merged, reporter = pull(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B."))],
        )

        assert merged == 0
        assert (collision_store / "decisions/003-local.md").exists()
        assert any("open question" in warning for warning in reporter.warns)

    def test_two_colliders_quarantine(self, collision_store):
        write_local_decision(collision_store, "003-a.md", decision_bytes(3, "A"))
        write_local_decision(collision_store, "003-b.md", decision_bytes(3, "B"))

        merged, reporter = pull(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B."))],
        )

        assert merged == 0
        assert any("more than one local file" in warning for warning in reporter.warns)

    def test_unparseable_collider_quarantines(self, collision_store):
        write_local_decision(collision_store, "003-local.md", b"not a decision at all\n")

        merged, reporter = pull(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B."))],
        )

        assert merged == 0
        assert (collision_store / "decisions/003-local.md").read_bytes() == (
            b"not a decision at all\n"
        )
        assert any("does not parse" in warning for warning in reporter.warns)

    def test_collider_without_rewritable_heading_quarantines(self, collision_store):
        # The decision parser tolerates extra whitespace after the hash; the
        # heading rewriter's canonical form does not. The file is otherwise
        # isolated, so only the unrewritable heading holds the renumber back.
        body = decision_bytes(3, "Local decision").replace(b"\n# 003", b"\n#  003")
        write_local_decision(collision_store, "003-local.md", body)

        merged, reporter = pull(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B."))],
        )

        assert merged == 0
        assert any("canonical form" in warning for warning in reporter.warns)
        # Nothing moved: the refusal happens before any rename.
        assert (collision_store / "decisions/003-local.md").read_bytes() == body

    def test_backup_written_once_across_repeated_pulls(self, collision_store):
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))
        track(collision_store, "decisions/003-local.md")
        remote = decision_bytes(3, "Remote decision", "Chose B.")

        for _ in range(3):
            merged, reporter = pull(collision_store, [("decisions/003-remote.md", remote)])
            assert merged == 0
            # The warning re-fires every pull; the backup does not multiply.
            assert len(reporter.warns) == 1

        backups = list_quarantine_backups(collision_store)
        assert len(backups) == 1
        assert backups[0].remote_path == "decisions/003-remote.md"
        assert backups[0].backup_path.read_bytes() == remote


class TestCanonicalize:
    def test_identical_unpublished_collider_is_renamed_onto_the_remote_name(self, collision_store):
        body = decision_bytes(3, "One decision")
        write_local_decision(collision_store, "003-local-name.md", body)

        merged, reporter = pull(collision_store, [("decisions/003-remote-name.md", body)])

        assert not (collision_store / "decisions/003-local-name.md").exists()
        assert (collision_store / "decisions/003-remote-name.md").read_bytes() == body
        state = load_state(collision_store)
        assert "decisions/003-remote-name.md" in state.files
        assert "decisions/003-local-name.md" not in state.files
        # Nothing was transferred onto disk, so nothing counts as merged - but
        # the run did act, so it must not sign off as "No remote changes".
        assert merged == 0
        assert reporter.warns == []
        assert any("two names" in info for info in reporter.infos)
        assert "Adopted 1 local file(s) already on the server" in reporter.infos
        assert "No remote changes" not in reporter.infos

    def test_crash_before_state_save_is_healed_by_the_adoption_leg(self, collision_store):
        """The canonicalize landed but the state save was lost: the next pull
        finds the exact file untracked and adopts it without a transfer."""
        body = decision_bytes(3, "One decision")
        write_local_decision(collision_store, "003-remote-name.md", body)

        merged, reporter = pull(collision_store, [("decisions/003-remote-name.md", body)])

        assert merged == 0
        assert (collision_store / "decisions/003-remote-name.md").read_bytes() == body
        assert "decisions/003-remote-name.md" in load_state(collision_store).files
        assert any("Adopted 1 local file" in info for info in reporter.infos)


class TestRenumberLocal:
    def test_isolated_collider_moves_and_the_remote_installs(self, collision_store):
        local = write_local_decision(
            collision_store, "003-local.md", decision_bytes(3, "Local decision")
        )
        remote = decision_bytes(3, "Remote decision", "Chose B.")

        merged, reporter = pull(collision_store, [("decisions/003-remote.md", remote)])

        assert merged == 1
        assert not local.exists()
        # The scaffold seeds 001, so the next free number is 004 once the
        # server's own 003 is reserved.
        renamed = collision_store / "decisions/004-local.md"
        assert renamed.read_bytes() == decision_bytes(4, "Local decision")
        assert (collision_store / "decisions/003-remote.md").read_bytes() == remote

        state = load_state(collision_store)
        assert "decisions/003-remote.md" in state.files
        # The renumbered local file was never published, so it stays untracked
        # and the next push uploads it.
        assert "decisions/004-local.md" not in state.files
        assert reporter.warns == []

    def test_mint_skips_numbers_the_server_already_holds(self, collision_store):
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))

        merged, _reporter = pull(
            collision_store,
            [
                ("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B.")),
                ("decisions/009-unrelated.md", decision_bytes(9, "Unrelated")),
            ],
        )

        assert merged == 2
        # 009 is in the manifest, so the local file may not mint onto it.
        assert (collision_store / "decisions/010-local.md").exists()
        assert (collision_store / "decisions/009-unrelated.md").exists()


class TestDualRemoteSameNumber:
    """Two remote decisions claiming one number: one installs, one quarantines."""

    @staticmethod
    def _entries(order):
        first = ("decisions/003-first.md", decision_bytes(3, "First remote", "Chose B."))
        second = ("decisions/003-second.md", decision_bytes(3, "Second remote", "Chose C."))
        return [first, second] if order == "forward" else [second, first]

    @pytest.mark.parametrize("order", ["forward", "reverse"])
    def test_exactly_one_installs(self, collision_store, order):
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))

        merged, reporter = pull(collision_store, self._entries(order))

        assert merged == 1
        installed = sorted(path.name for path in (collision_store / "decisions").glob("003-*.md"))
        assert len(installed) == 1
        assert len(reporter.warns) == 1
        # The loser's collider is the winner's freshly installed file, so the
        # quarantine is the published-versus-published one with no local route.
        assert "Nauro app" in reporter.warns[0]
        assert len(list_quarantine_backups(collision_store)) == 1


class TestAdoptionLeg:
    def test_identical_local_file_is_adopted_without_a_rewrite(self, collision_store):
        body = decision_bytes(5, "Already pushed")
        path = write_local_decision(collision_store, "005-already-pushed.md", body)

        merged, reporter = pull(collision_store, [("decisions/005-already-pushed.md", body)])

        assert merged == 0
        assert path.read_bytes() == body
        assert "decisions/005-already-pushed.md" in load_state(collision_store).files
        assert reporter.warns == []

    def test_differing_local_file_keeps_local_and_backs_up_remote(self, collision_store):
        local_body = decision_bytes(5, "Already pushed", "Local rationale.")
        remote_body = decision_bytes(5, "Already pushed", "Remote rationale.")
        path = write_local_decision(collision_store, "005-already-pushed.md", local_body)

        merged, _reporter = pull(
            collision_store, [("decisions/005-already-pushed.md", remote_body)]
        )

        assert merged == 1
        assert path.read_bytes() == local_body
        backups = list((collision_store / ".conflict-backup").iterdir())
        assert len(backups) == 1
        assert backups[0].read_bytes() == remote_body
        assert "decisions/005-already-pushed.md" in load_state(collision_store).files


class TestFreshStoreConvergence:
    """A store that pulls both the original and a previously renumbered copy
    records one file each and never re-collides."""

    @pytest.mark.parametrize("order", ["forward", "reverse"])
    def test_both_orders_converge(self, tmp_path, order):
        store = _scaffolded_cloud_project(f"fresh{order}", tmp_path)
        _seed_token()
        entries = [
            ("decisions/003-shared-slug.md", decision_bytes(3, "Shared slug", "Remote rationale.")),
            ("decisions/004-shared-slug.md", decision_bytes(4, "Shared slug", "Local rationale.")),
            ("decisions/010-unrelated.md", decision_bytes(10, "Unrelated")),
        ]
        if order == "reverse":
            entries.reverse()

        merged, reporter = pull(store, entries)

        assert merged == 3
        assert reporter.warns == []
        names = sorted(path.name for path in (store / "decisions").glob("*.md"))
        assert names == [
            "001-initial-setup.md",
            "003-shared-slug.md",
            "004-shared-slug.md",
            "010-unrelated.md",
        ]

        # A second pull of the same manifest is a no-op.
        merged_again, reporter_again = pull(store, entries)
        assert merged_again == 0
        assert reporter_again.warns == []


class TestEveryDecisionWriteIsGated:
    """The gate is at the write, not at the plan.

    A verdict taken while the run was deciding what to fetch is not a verdict:
    a second remote file can claim the same number, and a local writer can mint
    one in the meantime. Both are re-checked immediately before the write.
    """

    def test_two_remote_files_one_number_on_a_fresh_store(self, collision_store):
        merged, reporter = pull(
            collision_store,
            [
                ("decisions/003-first.md", decision_bytes(3, "First remote", "Chose A.")),
                ("decisions/003-second.md", decision_bytes(3, "Second remote", "Chose B.")),
            ],
        )

        assert merged == 1
        installed = sorted(path.name for path in (collision_store / "decisions").glob("003-*.md"))
        assert installed == ["003-first.md"]
        assert len(reporter.warns) == 1
        assert "Nauro app" in reporter.warns[0]
        assert len(list_quarantine_backups(collision_store)) == 1

    @pytest.mark.parametrize("order", ["forward", "reverse"])
    def test_the_manifest_order_picks_the_winner_deterministically(self, collision_store, order):
        entries = [
            ("decisions/003-first.md", decision_bytes(3, "First remote", "Chose A.")),
            ("decisions/003-second.md", decision_bytes(3, "Second remote", "Chose B.")),
        ]
        if order == "reverse":
            entries.reverse()

        merged, _reporter = pull(collision_store, entries)

        assert merged == 1
        installed = sorted(path.name for path in (collision_store / "decisions").glob("003-*.md"))
        assert installed == [entries[0][0].split("/")[1]]

    def test_a_number_minted_after_the_plan_is_caught_before_the_write(self, collision_store):
        """A local writer mints the contested number between the plan and the
        write; the gate re-runs and applies the matrix instead of duplicating."""
        remote = decision_bytes(5, "Remote decision", "Chose B.")
        mint = decision_bytes(5, "Locally minted", "Chose C.")

        real_scan = DecisionCorpus.scan
        calls = {"n": 0}

        def scan_then_mint(store_path):
            # The first scan is the planner's; the writer's scan must see the
            # file that landed in between.
            corpus = real_scan(store_path)
            calls["n"] += 1
            if calls["n"] == 1:
                write_local_decision(collision_store, "005-locally-minted.md", mint)
            return corpus

        with patch.object(DecisionCorpus, "scan", staticmethod(scan_then_mint)):
            merged, reporter = pull(collision_store, [("decisions/005-remote.md", remote)])

        assert merged == 1
        # The late mint was renumbered out of the way rather than duplicated.
        assert (collision_store / "decisions/005-remote.md").read_bytes() == remote
        numbers = sorted(
            int(path.name.split("-")[0]) for path in (collision_store / "decisions").glob("*.md")
        )
        assert len(numbers) == len(set(numbers))
        assert reporter.warns == []


class TestAdoptionSiblings:
    def test_a_sibling_holding_the_number_is_renumbered_before_adopting(self, collision_store):
        body = decision_bytes(5, "Already pushed")
        exact = write_local_decision(collision_store, "005-already-pushed.md", body)
        sibling = write_local_decision(
            collision_store, "005-other.md", decision_bytes(5, "Other", "Chose C.")
        )

        merged, reporter = pull(collision_store, [("decisions/005-already-pushed.md", body)])

        assert exact.read_bytes() == body
        assert not sibling.exists()
        assert (collision_store / "decisions/006-other.md").exists()
        assert "decisions/005-already-pushed.md" in load_state(collision_store).files
        assert merged == 0
        assert reporter.warns == []

    def test_an_unresolvable_sibling_blocks_the_adoption(self, collision_store):
        body = decision_bytes(5, "Already pushed")
        write_local_decision(collision_store, "005-already-pushed.md", body)
        sibling = write_local_decision(
            collision_store, "005-other.md", decision_bytes(5, "Other", "Chose C.")
        )
        track(collision_store, "decisions/005-other.md")

        merged, reporter = pull(collision_store, [("decisions/005-already-pushed.md", body)])

        assert merged == 0
        assert sibling.exists()
        # No state recorded, so the warning re-fires until the duplicate is gone.
        assert "decisions/005-already-pushed.md" not in load_state(collision_store).files
        assert len(reporter.warns) == 1
        assert "005-other.md" in reporter.warns[0]

    def test_a_case_only_name_difference_is_never_adopted(self, collision_store):
        """On a case-folding filesystem the remote name resolves to the local
        file, so writing it would overwrite content instead of adding a record.
        The listing's own spelling decides, not the OS, and the mismatch is
        handed to the human rather than renamed automatically."""
        local_body = decision_bytes(7, "Local casing", "Chose A.")
        local = write_local_decision(collision_store, "007-Thing.md", local_body)
        remote = decision_bytes(7, "Remote casing", "Chose B.")

        merged, reporter = pull(collision_store, [("decisions/007-thing.md", remote)])

        assert merged == 0
        assert local.read_bytes() == local_body
        assert "decisions/007-thing.md" not in load_state(collision_store).files
        assert any("letter case" in warning for warning in reporter.warns)
        assert len(list_quarantine_backups(collision_store)) == 1

    def test_an_unnumbered_case_variant_is_quarantined(self, collision_store):
        # The remote name is one the writer could have minted; only the local
        # file's spelling differs, so this is the case-variant cell rather than
        # the non-canonical one.
        write_local_decision(collision_store, "NOTES.md", b"local notes\n")

        merged, reporter = pull(collision_store, [("decisions/notes.md", b"remote notes\n")])

        assert merged == 0
        assert "notes.md" not in entry_names(collision_store / "decisions")
        assert any("letter case" in warning for warning in reporter.warns)


class TestIrregularEntries:
    """A directory or symlink named like a decision is never read or changed."""

    def test_a_directory_named_like_a_decision_does_not_crash_the_pull(self, collision_store):
        (collision_store / "decisions" / "009-broken.md").mkdir(parents=True)

        merged, reporter = pull(
            collision_store, [("decisions/010-fine.md", decision_bytes(10, "Fine"))]
        )

        assert merged == 1
        assert (collision_store / "decisions/010-fine.md").exists()
        assert any("not a regular file" in warning for warning in reporter.warns)

    def test_a_broken_symlink_does_not_crash_the_pull(self, collision_store):
        link = collision_store / "decisions" / "011-dangling.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(collision_store / "decisions" / "nothing-here.md")

        merged, reporter = pull(
            collision_store, [("decisions/012-fine.md", decision_bytes(12, "Fine"))]
        )

        assert merged == 1
        assert any("not a regular file" in warning for warning in reporter.warns)

    def test_a_remote_decision_claiming_a_symlinked_number_is_quarantined(self, collision_store):
        target = write_local_decision(
            collision_store, "013-target.md", decision_bytes(13, "Target")
        )
        link = collision_store / "decisions" / "014-link.md"
        link.symlink_to(target)

        merged, reporter = pull(
            collision_store, [("decisions/014-remote.md", decision_bytes(14, "Remote"))]
        )

        assert merged == 0
        assert not (collision_store / "decisions/014-remote.md").exists()
        assert link.is_symlink()
        assert any("not a regular file" in warning for warning in reporter.warns)

    def test_reserved_frontmatter_keys_quarantine_instead_of_crashing(self, collision_store):
        # `num` is a decision-model constructor argument, so a file carrying it
        # raises TypeError rather than ValueError when parsed.
        poisoned = decision_bytes(3, "Local").replace(b"supersedes: null", b"num: 99")
        write_local_decision(collision_store, "003-local.md", poisoned)

        merged, reporter = pull(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote", "Chose B."))],
        )

        assert merged == 0
        assert (collision_store / "decisions/003-local.md").read_bytes() == poisoned
        assert any("does not parse" in warning for warning in reporter.warns)


class TestTransferShape:
    """Decision files are batched; everything else streams.

    The gate needs the fetched bytes of every decision file it will judge, so
    those are held together for the length of one lock hold. Snapshots and
    briefs have no such requirement, and a first pull of a real store is
    hundreds of megabytes of them: they are fetched and written one at a time,
    outside the lock, so neither memory nor a local writer pays for the batch.
    """

    def _snapshots(self, count: int) -> list[tuple[str, bytes]]:
        return [(f"snapshots/v{n:03d}.json", f'{{"v": {n}}}'.encode()) for n in range(1, count + 1)]

    def test_non_decision_transfers_are_written_before_the_next_is_fetched(self, collision_store):
        entries = self._snapshots(3)
        seen_on_disk: list[int] = []
        real_fetch = pull_module.fetch_via_presigned_url

        def counting_fetch(url):
            seen_on_disk.append(len(list((collision_store / "snapshots").glob("*.json"))))
            return real_fetch(url)

        with patch.object(pull_module, "fetch_via_presigned_url", counting_fetch):
            merged, _reporter = pull(collision_store, entries)

        assert merged == 3
        # Each fetch happens after the previous file already landed, so at most
        # one transfer is in memory at a time.
        assert seen_on_disk == [0, 1, 2]

    def test_the_decision_lock_is_free_while_non_decision_files_are_written(self, collision_store):
        acquired: list[bool] = []
        real_fetch = pull_module.fetch_via_presigned_url

        def probing_fetch(url):
            try:
                with decision_lock(collision_store, 0.05):
                    acquired.append(True)
            except SyncLockTimeoutError:
                acquired.append(False)
            return real_fetch(url)

        with patch.object(pull_module, "fetch_via_presigned_url", probing_fetch):
            merged, _reporter = pull(collision_store, self._snapshots(2))

        assert merged == 2
        assert acquired == [True, True]

    def test_the_decision_lock_is_held_while_a_decision_is_classified(self, collision_store):
        held: list[bool] = []
        real_classify = pull_module.classify_decision

        def probing_classify(*args, **kwargs):
            try:
                with decision_lock(collision_store, 0.05):
                    held.append(False)
            except SyncLockTimeoutError:
                held.append(True)
            return real_classify(*args, **kwargs)

        with patch.object(pull_module, "classify_decision", probing_classify):
            merged, _reporter = pull(
                collision_store, [("decisions/003-remote.md", decision_bytes(3, "Remote"))]
            )

        assert merged == 1
        assert held == [True]


class TestManifestPathCasing:
    """Only one spelling of a decision path is ever written.

    The pull routes every case spelling to the classifier, because a manifest
    entry spelled ``Decisions/007-x.MD`` names the same local file as
    ``decisions/007-x.md`` wherever the filesystem folds case, and letting it
    fall through to the untyped path dropped it silently. But routing is not
    permission: everything else that enumerates the directory - the corpus
    scan, ``list_decisions``, the kernel's number allocation - matches
    lowercase ``decisions/`` and ``.md`` literally, so a file written under any
    other spelling would be invisible to the allocator that must not reuse its
    number, and state recorded under it would not match the path the push scan
    reports. Non-canonical spellings are quarantined, never installed.
    """

    @pytest.mark.parametrize("remote_path", ["decisions/007-thing.MD", "Decisions/007-thing.md"])
    def test_non_canonical_paths_aliasing_a_local_file_are_quarantined(
        self, collision_store, remote_path
    ):
        local_body = decision_bytes(7, "Local casing", "Chose A.")
        local = write_local_decision(collision_store, "007-Thing.md", local_body)

        merged, reporter = pull(
            collision_store, [(remote_path, decision_bytes(7, "Remote casing", "Chose B."))]
        )

        assert merged == 0
        assert local.read_bytes() == local_body
        assert remote_path.split("/")[1] not in entry_names(collision_store / "decisions")
        assert "Decisions" not in entry_names(collision_store)
        assert load_state(collision_store).files == {}
        assert any("decisions/<name>.md" in warning for warning in reporter.warns)

    def test_a_non_canonical_path_never_reaches_the_disk_or_the_allocator(self, collision_store):
        """Nothing local claims the number, so the only thing stopping an
        install is the spelling - and it has to, because a file the allocator
        cannot see is a number it will hand out again."""
        before = FilesystemStore(collision_store).list_decisions()

        merged, reporter = pull(
            collision_store, [("decisions/002-remote.MD", decision_bytes(2, "Remote"))]
        )

        assert merged == 0
        assert "002-remote.MD" not in entry_names(collision_store / "decisions")
        assert load_state(collision_store).files == {}
        assert any("decisions/<name>.md" in warning for warning in reporter.warns)
        # The allocator sees exactly what it saw before, so its next mint is
        # the same number it would have minted without this pull.
        store = FilesystemStore(collision_store)
        assert store.list_decisions() == before
        assert _next_decision_num(store) == 2

    def test_a_non_canonical_directory_does_not_block_its_canonical_neighbours(
        self, collision_store
    ):
        merged, reporter = pull(
            collision_store,
            [
                ("Decisions/031-elsewhere.md", decision_bytes(31, "Elsewhere")),
                ("decisions/032-canonical.md", decision_bytes(32, "Canonical")),
            ],
        )

        assert merged == 1
        assert "Decisions" not in entry_names(collision_store)
        assert (collision_store / "decisions/032-canonical.md").exists()
        assert list(load_state(collision_store).files) == ["decisions/032-canonical.md"]
        assert len(reporter.warns) == 1
        assert len(list_quarantine_backups(collision_store)) == 1


class TestDecisionBatchSpool:
    """The gated batch is complete before the lock, but never resident.

    A manifest is server-supplied input; its total size is not something this
    process may be surprised by. Decision bodies are parked on disk between the
    fetch and the classification, so the batch costs one file's memory.
    """

    def _entries(self, count: int) -> list[tuple[str, bytes]]:
        return [
            (f"decisions/{n:03d}-remote.md", decision_bytes(n, f"Remote {n}"))
            for n in range(10, 10 + count)
        ]

    def _spool_dirs(self, store) -> list[str]:
        return [name for name in entry_names(store) if name.startswith(SPOOL_DIR_PREFIX)]

    def test_each_body_is_on_disk_before_the_next_is_fetched(self, collision_store):
        spooled_before_fetch: list[int] = []
        real_fetch = pull_module.fetch_via_presigned_url

        def counting_fetch(url):
            spooled = self._spool_dirs(collision_store)
            spooled_before_fetch.append(
                len(list((collision_store / spooled[0]).iterdir())) if spooled else 0
            )
            return real_fetch(url)

        with patch.object(pull_module, "fetch_via_presigned_url", counting_fetch):
            merged, _reporter = pull(collision_store, self._entries(3))

        assert merged == 3
        assert spooled_before_fetch == [0, 1, 2]

    def test_the_spool_is_removed_when_the_run_finishes(self, collision_store):
        merged, _reporter = pull(collision_store, self._entries(2))

        assert merged == 2
        assert self._spool_dirs(collision_store) == []

    def test_the_spool_is_removed_when_the_run_fails(self, collision_store):
        boom = RuntimeError("classification exploded")

        with (
            patch.object(pull_module, "classify_decision", side_effect=boom),
            pytest.raises(RuntimeError),
        ):
            pull(collision_store, self._entries(2))

        assert self._spool_dirs(collision_store) == []

    def test_a_stray_spool_directory_is_never_synced(self, collision_store):
        assert should_skip(f"{SPOOL_DIR_PREFIX}abc123/000000") is True
        assert should_skip(f"{SPOOL_DIR_PREFIX}abc123") is True


# Every spelling a manifest could use for something under decisions/, and
# whether this store is willing to write it. The rejected forms are the point:
# three of them normalise onto a real local decision, one installs where the
# allocator cannot see it, and one is stripped to a real path by Windows.
_SPELLINGS = [
    ("decisions/240-fine.md", True),
    ("decisions/sub/240-fine.md", False),
    ("decisions//240-fine.md", False),
    ("decisions/./240-fine.md", False),
    ("decisions/../240-fine.md", False),
    ("decisions/240-fine.md/", False),
    ("decisions/240-fine.md.", False),
    ("decisions/240-fine.MD", False),
    ("Decisions/240-fine.md", False),
    ("decisions/notes.txt", False),
]


class TestDecisionPathSpelling:
    """Only a flat, lowercase ``decisions/<name>.md`` is ever written.

    A suffix test is not enough: ``decisions//x.md``, ``decisions/./x.md`` and
    ``decisions/x.md/`` all resolve to the same file as ``decisions/x.md``, so
    accepting them would overwrite a decision without the classifier ever
    comparing it; ``decisions/sub/x.md`` installs a file the corpus and the
    kernel's allocator never enumerate but the push scan still uploads; and
    Windows strips the trailing dot from ``decisions/x.md.`` onto a real name.
    """

    @staticmethod
    def _disk(store) -> dict[str, bytes]:
        """Every store file that is content, not sync plumbing."""
        return {
            rel: path.read_bytes()
            for path in sorted(store.rglob("*"))
            if path.is_file()
            and ".conflict-backup" not in path.parts
            and not should_skip(rel := str(path.relative_to(store)))
        }

    @pytest.mark.parametrize(("remote_path", "accepted"), _SPELLINGS)
    def test_only_the_canonical_spelling_is_written(self, collision_store, remote_path, accepted):
        local_body = decision_bytes(240, "Local", "Chose A.")
        write_local_decision(collision_store, "240-fine.md", local_body)
        before = self._disk(collision_store)

        merged, reporter = pull(
            collision_store, [(remote_path, decision_bytes(240, "Remote", "Chose B."))]
        )

        if accepted:
            # The canonical spelling reaches the matrix, which sees an
            # untracked local file of that name and applies the existing
            # last-write-wins policy: local kept, remote backed up, state
            # recorded under the path the store actually enumerates.
            assert merged == 1
            assert (collision_store / remote_path).read_bytes() == local_body
            assert list(load_state(collision_store).files) == [remote_path]
            return
        assert merged == 0
        # Nothing outside the backup drop-box moved: in particular the local
        # decision the aliasing forms would have overwritten.
        assert self._disk(collision_store) == before
        assert (collision_store / "decisions/240-fine.md").read_bytes() == local_body
        assert load_state(collision_store).files == {}

    def test_a_traversal_spelling_is_refused_before_it_is_even_fetched(self, collision_store):
        """The manifest guard already drops it, so it never reaches the gate
        and there is nothing to back up."""
        merged, reporter = pull(collision_store, [("decisions/../240-fine.md", b"remote body\n")])

        assert merged == 0
        assert list_quarantine_backups(collision_store) == []
        assert any("suspicious" in warning for warning in reporter.warns)

    @pytest.mark.parametrize(
        "remote_path",
        # The traversal form never gets this far; it has its own test above.
        [path for path, ok in _SPELLINGS if not ok and ".." not in path],
    )
    def test_a_rejected_spelling_is_named_accurately_in_its_backup(
        self, collision_store, remote_path
    ):
        merged, _reporter = pull(collision_store, [(remote_path, b"remote body\n")])

        assert merged == 0
        backups = list_quarantine_backups(collision_store)
        # A lossy backup name would have status report a path the server never
        # held; the separators survive the round trip.
        assert [item.remote_path for item in backups] == [remote_path]
        assert backups[0].backup_path.read_bytes() == b"remote body\n"
        assert unresolved_quarantines(collision_store) == backups

    @pytest.mark.parametrize(
        "filename",
        [
            "240-x:stream.md",  # a colon opens an alternate data stream on NTFS
            "CON.md",  # a reserved device basename, whatever the extension
            "con.md",
            "com1.md",
            "240-Mixed.md",  # the writer lowercases every name it mints
            ".md",  # no stem at all
            "..md",
        ],
    )
    def test_a_filename_the_writer_could_not_have_minted_is_refused(
        self, collision_store, filename
    ):
        """The filename rule is an allowlist, not a denylist of the characters
        that have bitten other tools: that set is not knowable in advance, and
        the set this store's own writer emits is."""
        before = self._disk(collision_store)

        merged, reporter = pull(collision_store, [(f"decisions/{filename}", b"remote\n")])

        assert merged == 0
        assert self._disk(collision_store) == before
        assert any("decisions/<name>.md" in warning for warning in reporter.warns)

    @pytest.mark.parametrize(
        "filename", ["240-fine.md", "001-initial-setup.md", "240-caf\u00e9-choice.md", "240-a_b.md"]
    )
    def test_names_the_writer_does_mint_are_admitted(self, collision_store, filename):
        """Including a slug in another script: the writer keeps alphanumerics
        whatever their alphabet, so refusing them would quarantine decisions
        Nauro itself wrote."""
        merged, reporter = pull(
            collision_store, [(f"decisions/{filename}", decision_bytes(240, "Remote"))]
        )

        assert merged == 1
        assert filename in entry_names(collision_store / "decisions")
        assert reporter.warns == []


class TestTrackedNonCanonicalPaths:
    """A state entry is not a licence to skip the gate."""

    def test_a_tracked_non_canonical_path_is_quarantined_on_change(self, collision_store):
        legacy = "decisions/002-old.MD"
        local = write_local_decision(collision_store, "002-old.MD", b"legacy local\n")
        state = SyncState()
        state.files[legacy] = FileState(
            local_sha256=compute_sha256(local),
            remote_etag='"old"',
            last_sync="2026-08-10T00:00:00Z",
        )
        save_state(collision_store, state)

        merged, reporter = pull(
            collision_store, [(legacy, b"new remote body\n")], etags={legacy: '"changed"'}
        )

        assert merged == 0
        assert local.read_bytes() == b"legacy local\n"
        # The stale entry is left exactly as it was: not refreshed, not removed.
        assert load_state(collision_store).files[legacy].remote_etag == '"old"'
        assert any("decisions/<name>.md" in warning for warning in reporter.warns)

    def test_the_stale_entry_does_not_mark_the_quarantine_resolved(self, collision_store):
        """Resolution keys on a canonical path, because only a canonical path
        can ever be installed. The legacy entry is the stale record the
        quarantine exists to surface, so it cannot clear its own warning."""
        legacy = "decisions/002-old.MD"
        write_local_decision(collision_store, "002-old.MD", b"legacy local\n")
        state = SyncState()
        state.files[legacy] = FileState(local_sha256="stale", remote_etag='"old"')
        save_state(collision_store, state)

        pull(collision_store, [(legacy, b"new remote body\n")], etags={legacy: '"changed"'})

        unresolved = unresolved_quarantines(collision_store)
        assert [item.remote_path for item in unresolved] == [legacy]


class TestWriteBarrier:
    """The guard is where the bytes land, not where the manifest spelled it.

    A spelling predicate can only refuse what it was taught to recognise, and
    the ways a path can name a decision without looking like one are open
    ended: ``./decisions/x.md`` normalises into the directory, a backslash key
    is a separator on Windows and the push scan emits them there, a symlink
    points wherever it points. Resolving the destination answers the question
    the predicate was only approximating.
    """

    @staticmethod
    def _content(store) -> dict[str, bytes]:
        return {
            rel: path.read_bytes()
            for path in sorted(store.rglob("*"))
            if path.is_file()
            and ".conflict-backup" not in path.parts
            and not should_skip(rel := str(path.relative_to(store)))
        }

    @pytest.mark.parametrize(
        "remote_path",
        ["./decisions/240-local.md", ".//decisions/240-local.md", "decisions\\240-local.md"],
    )
    def test_a_path_that_resolves_into_decisions_never_writes_generically(
        self, collision_store, remote_path
    ):
        local_body = decision_bytes(240, "Local", "Chose A.")
        write_local_decision(collision_store, "240-local.md", local_body)
        before = self._content(collision_store)

        merged, reporter = pull(
            collision_store, [(remote_path, decision_bytes(240, "Remote", "Chose B."))]
        )

        assert merged == 0
        assert self._content(collision_store) == before
        assert (collision_store / "decisions/240-local.md").read_bytes() == local_body
        assert load_state(collision_store).files == {}
        # Quarantined under the spelling the server actually used.
        assert [item.remote_path for item in list_quarantine_backups(collision_store)] == [
            remote_path
        ]

    def test_a_destination_outside_the_store_is_refused(self, collision_store):
        # A symlink is the shape that survives the manifest guard: the path has
        # no dot segments, so only resolving it reveals where it points.
        outside = collision_store.parent / "outside"
        outside.mkdir()
        (outside / "victim.md").write_bytes(b"untouched\n")
        (collision_store / "escape").symlink_to(outside)

        merged, reporter = pull(collision_store, [("escape/victim.md", b"overwritten\n")])

        assert merged == 0
        assert (outside / "victim.md").read_bytes() == b"untouched\n"
        assert any("outside the store" in warning for warning in reporter.warns)

    def test_ordinary_files_are_unaffected(self, collision_store):
        merged, reporter = pull(
            collision_store,
            [("snapshots/v001.json", b'{"v": 1}'), ("context/brief.md", b"# Brief\n")],
        )

        assert merged == 2
        assert (collision_store / "snapshots/v001.json").read_bytes() == b'{"v": 1}'
        assert (collision_store / "context/brief.md").read_bytes() == b"# Brief\n"
        assert reporter.warns == []

    @pytest.mark.parametrize(
        "rel",
        ["./decisions/x.md", ".//decisions/x.md", "decisions\\x.md", "decisions/x.md"],
    )
    def test_the_barrier_itself_refuses_every_decision_destination(self, collision_store, rel):
        """The barrier is asserted directly, not through a contrived planner:
        it is the last check before bytes land and has to hold on its own."""
        allowed = pull_module._generic_write_allowed(
            collision_store, _Transfer(rel=rel, etag='"e"', _body=b""), _RecordingReporter()
        )

        assert allowed is False

    def test_the_barrier_admits_an_ordinary_store_path(self, collision_store):
        allowed = pull_module._generic_write_allowed(
            collision_store,
            _Transfer(rel="snapshots/v001.json", etag='"e"', _body=b""),
            _RecordingReporter(),
        )

        assert allowed is True


class TestBackupIdentity:
    def test_two_spellings_of_one_path_keep_separate_backups(self, collision_store):
        """On a case-folding filesystem a name derived from the path alone
        collapses, so one quarantine would overwrite the other's evidence and
        installing the canonical file would appear to resolve both."""
        etag = '"same-version"'
        # A published local file holding the number, so the canonical spelling
        # quarantines too and both copies are kept at the same remote version.
        write_local_decision(collision_store, "007-local.md", decision_bytes(7, "Local"))
        track(collision_store, "decisions/007-local.md")
        spellings = ["decisions/007-remote.md", "Decisions/007-Remote.MD"]

        merged, _reporter = pull(
            collision_store,
            [(path, decision_bytes(7, "Remote")) for path in spellings],
            etags=dict.fromkeys(spellings, etag),
        )

        assert merged == 0
        backups = list_quarantine_backups(collision_store)
        names = {item.backup_path.name for item in backups}
        assert len(names) == 2
        # Distinct without relying on the host filesystem's case behaviour.
        assert len({name.lower() for name in names}) == 2
        assert {item.remote_path for item in backups} == set(spellings)

    def test_installing_the_canonical_file_clears_only_its_own_quarantine(self, collision_store):
        state = SyncState()
        state.files["decisions/007-remote.md"] = FileState(
            local_sha256="abc", remote_etag='"installed"'
        )
        save_state(collision_store, state)
        save_quarantine_backup(collision_store, "decisions/007-remote.md", b"one\n", '"a"')
        save_quarantine_backup(collision_store, "Decisions/007-Remote.MD", b"two\n", '"a"')

        unresolved = unresolved_quarantines(collision_store)

        assert [item.remote_path for item in unresolved] == ["Decisions/007-Remote.MD"]


class TestDirectoryDestinations:
    """A manifest entry naming a directory is not a file, however it is spelled.

    ``decisions`` is the one that matters: written as an ordinary file it
    either crashes the local-change probe on a directory hash or, on a store
    that has none yet, creates a regular file under the name every future
    decision write needs.
    """

    @pytest.mark.parametrize("remote_path", ["decisions", "decisions/"])
    @pytest.mark.parametrize("directory_exists", [True, False])
    def test_the_decisions_directory_is_never_written_as_a_file(
        self, collision_store, remote_path, directory_exists
    ):
        decisions = collision_store / "decisions"
        if not directory_exists:
            for child in decisions.iterdir():
                child.unlink()
            decisions.rmdir()

        merged, reporter = pull(collision_store, [(remote_path, b"not a directory\n")])

        assert merged == 0
        assert reporter.warns
        # Whether it was there to begin with or the decision lock recreated it,
        # the name is a directory and never a regular file: a file there would
        # block every future decision write.
        assert not decisions.is_file()
        assert decisions.is_dir() or not decisions.exists()

    def test_another_directory_destination_is_skipped_not_hashed(self, collision_store):
        (collision_store / "snapshots").mkdir(exist_ok=True)

        merged, reporter = pull(collision_store, [("snapshots", b"not a directory\n")])

        assert merged == 0
        assert (collision_store / "snapshots").is_dir()
        assert any("directory" in warning for warning in reporter.warns)
