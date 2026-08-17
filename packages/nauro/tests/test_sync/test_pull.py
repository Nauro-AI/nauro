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

import hashlib
import os
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from nauro_core import extract_decision_number
from nauro_core.operations.propose_decision import _next_decision_num

from nauro.auth import AuthRefreshError
from nauro.constants import DECISIONS_DIR
from nauro.store import _atomic
from nauro.store._atomic import atomic_write_bytes, is_tmp_sibling
from nauro.store.filesystem_store import FilesystemStore
from nauro.sync import pull as pull_module
from nauro.sync.corpus import DecisionCorpus
from nauro.sync.lock import SyncLockTimeoutError, decision_lock
from nauro.sync.merge import CONFLICT_BACKUP_DIR, SPOOL_DIR_PREFIX, should_skip
from nauro.sync.pull import PullReport, _Transfer, run_pull
from nauro.sync.quarantine import (
    list_quarantine_backups,
    save_quarantine_backup,
    unresolved_quarantines,
)
from nauro.sync.remote import PresignError
from nauro.sync.state import (
    FileState,
    SyncState,
    compute_sha256,
    load_state,
    save_state,
)
from tests.test_sync.conftest import (
    _STAMPED_DECISION,
    CLOUD_PID,
    _manifest,
    _presign,
    _RecordingReporter,
    _scaffolded_cloud_project,
    _seed_token,
    decision_bytes,
    entry_names,
    pull,
    pull_report,
    track,
    write_local_decision,
)

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
            patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.Client.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, reporter).merged

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
            patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.Client.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, _RecordingReporter()).merged

        assert merged == 1
        assert (cloud_store / rel).read_bytes() == _STAMPED_DECISION

    def test_remote_snapshot_is_ignored_before_presign(self, cloud_store):
        rel = "snapshots/v001.json"
        original = FileState(
            local_sha256="pruned-local-sha",
            remote_etag='"old"',
            last_sync="2026-05-16T00:00:00Z",
        )
        state = SyncState(files={rel: original})
        save_state(cloud_store, state)
        manifest = _manifest([{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}])

        reporter = _RecordingReporter()
        with (
            patch("nauro.sync.remote.httpx.Client.get", return_value=manifest),
            patch("nauro.sync.remote.httpx.Client.post") as mock_post,
        ):
            report = run_pull(CLOUD_PID, cloud_store, reporter)

        assert report == PullReport()
        mock_post.assert_not_called()
        assert not (cloud_store / rel).exists()
        assert load_state(cloud_store).files[rel] == original
        assert reporter.infos == ["No remote changes"]
        assert reporter.warns == []

    def test_unsafe_snapshot_path_is_still_refused(self, cloud_store):
        rel = "snapshots/../../outside.json"
        manifest = _manifest([{"path": rel, "etag": '"new"', "size": 1, "last_modified": "x"}])

        reporter = _RecordingReporter()
        with (
            patch("nauro.sync.remote.httpx.Client.get", return_value=manifest),
            patch("nauro.sync.remote.httpx.Client.post") as mock_post,
        ):
            report = run_pull(CLOUD_PID, cloud_store, reporter)

        assert report == PullReport(skipped_permanent=1)
        mock_post.assert_not_called()
        assert any("suspicious manifest entry" in warning for warning in reporter.warns)

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
            patch("nauro.sync.remote.httpx.Client.get", side_effect=fake_get),
            patch("nauro.sync.remote.httpx.Client.post", return_value=presign),
        ):
            merged = run_pull(CLOUD_PID, cloud_store, reporter).merged

        assert merged == 1
        merged_bytes = local.read_bytes()
        # Union of both sides — neither entry was dropped.
        assert b"local entry" in merged_bytes
        assert b"remote entry" in merged_bytes
        assert compute_sha256(local) != local_sha
        assert reporter.warns == []


# --- decision-number collisions: one classification per remote file ---


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


class TestNonAsciiFilenameNumbers:
    """A local file whose name carries a non-ASCII number is not a decision.

    The corpus reads a number from every name in the directory, and the read
    used to raise on a character that ``str.isdigit`` accepts but ``int`` does
    not. That happens while the pull is still planning, before the lock and
    before the per-file guard, so one such file aborted the whole sync.
    """

    def test_a_file_with_a_non_ascii_number_does_not_crash_the_pull(self, collision_store):
        stray = collision_store / "decisions" / "\u00b2-x.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(decision_bytes(2, "Stray"))

        merged, reporter = pull(
            collision_store, [("decisions/017-fine.md", decision_bytes(17, "Fine"))]
        )

        assert merged == 1
        assert (collision_store / "decisions/017-fine.md").exists()
        assert stray.read_bytes() == decision_bytes(2, "Stray")
        assert reporter.warns == []

    def test_it_claims_no_number_so_a_remote_decision_still_installs(self, collision_store):
        # An Arabic-Indic name reads as 12 to the old parser, which would have
        # made this file a collider for a number it does not spell.
        stray = collision_store / "decisions" / "\u0661\u0662-y.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(decision_bytes(12, "Stray"))

        merged, _reporter = pull(
            collision_store, [("decisions/012-remote.md", decision_bytes(12, "Remote"))]
        )

        assert merged == 1
        assert (collision_store / "decisions/012-remote.md").exists()
        assert stray.read_bytes() == decision_bytes(12, "Stray")

    def test_a_number_continuing_in_another_script_contests_nothing(self, collision_store):
        # The reader used to stop at the character it could not read and call
        # this decision 12, so it contested a real decision it does not name.
        stray = collision_store / "decisions" / "12\u0661-x.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(decision_bytes(12, "Stray"))

        corpus = DecisionCorpus.scan(collision_store)
        assert corpus.holders(12) == ()
        assert "12\u0661-x.md" not in {entry.path.name for entry in corpus.files}

        merged, _reporter = pull(
            collision_store, [("decisions/012-remote.md", decision_bytes(12, "Remote"))]
        )

        assert merged == 1
        assert (collision_store / "decisions/012-remote.md").exists()
        assert stray.read_bytes() == decision_bytes(12, "Stray")

    def test_the_corpus_scan_skips_it(self, collision_store):
        stray = collision_store / "decisions" / "\u00b2-x.md"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(decision_bytes(2, "Stray"))

        corpus = DecisionCorpus.scan(collision_store)

        assert "\u00b2-x.md" not in {entry.path.name for entry in corpus.files}
        # A plain skip, not an irregular entry: those exist to hold back a
        # remote decision claiming the number a name occupies, and this name
        # claims none.
        assert corpus.irregular == ()
        # It is still a name the store knows about, so a remote file cannot
        # land on it unseen.
        assert corpus.has_name("\u00b2-x.md") is True


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
    those are held together for the length of one lock hold. Other store files
    have no such requirement. They are fetched and written one at a time,
    outside the lock, so neither memory nor a local writer pays for the batch.
    """

    def _ordinary_files(self, count: int) -> list[tuple[str, bytes]]:
        return [(f"stream-{n:03d}.json", f'{{"v": {n}}}'.encode()) for n in range(1, count + 1)]

    def test_non_decision_transfers_are_written_before_the_next_is_fetched(self, collision_store):
        entries = self._ordinary_files(3)
        seen_on_disk: list[int] = []
        real_fetch = pull_module.fetch_via_presigned_url

        def counting_fetch(url, **kwargs):
            seen_on_disk.append(len(list(collision_store.glob("stream-*.json"))))
            return real_fetch(url, **kwargs)

        with patch.object(pull_module, "fetch_via_presigned_url", counting_fetch):
            merged, _reporter = pull(collision_store, entries)

        assert merged == 3
        # Each fetch happens after the previous file already landed, so at most
        # one transfer is in memory at a time.
        assert seen_on_disk == [0, 1, 2]

    def test_the_decision_lock_is_free_while_non_decision_files_are_written(self, collision_store):
        acquired: list[bool] = []
        real_fetch = pull_module.fetch_via_presigned_url

        def probing_fetch(url, **kwargs):
            try:
                with decision_lock(collision_store, 0.05):
                    acquired.append(True)
            except SyncLockTimeoutError:
                acquired.append(False)
            return real_fetch(url, **kwargs)

        with patch.object(pull_module, "fetch_via_presigned_url", probing_fetch):
            merged, _reporter = pull(collision_store, self._ordinary_files(2))

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

        def counting_fetch(url, **kwargs):
            spooled = self._spool_dirs(collision_store)
            spooled_before_fetch.append(
                len(list((collision_store / spooled[0]).iterdir())) if spooled else 0
            )
            return real_fetch(url, **kwargs)

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
            [("metrics.json", b'{"v": 1}'), ("context/brief.md", b"# Brief\n")],
        )

        assert merged == 2
        assert (collision_store / "metrics.json").read_bytes() == b'{"v": 1}'
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
            collision_store,
            _Transfer(rel=rel, etag='"e"', _body=b""),
            SyncState(),
            _RecordingReporter(),
        )

        assert allowed is False

    def test_the_barrier_admits_an_ordinary_store_path(self, collision_store):
        allowed = pull_module._generic_write_allowed(
            collision_store,
            _Transfer(rel="metrics.json", etag='"e"', _body=b""),
            SyncState(),
            _RecordingReporter(),
        )

        assert allowed is True


class TestQuarantineBackupSurvivesADeadWrite:
    """A backup that dies mid-write must not look like a finished one.

    The quarantine writer skips a backup that already exists for the same
    remote version, so a truncated file under the right name would be kept
    forever - and it is the only copy of the bytes the pull declined to
    install.
    """

    def _quarantining_pull(self, store, reporter=None):
        remote = decision_bytes(3, "Remote decision", "Chose B.")
        return pull(store, [("decisions/003-remote.md", remote)], reporter=reporter)

    def test_the_next_pull_writes_the_backup_the_dead_write_did_not(
        self, collision_store, monkeypatch
    ):
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))
        track(collision_store, "decisions/003-local.md")
        backup_dir = collision_store / ".conflict-backup"
        real_replace = os.replace
        dying = {"first run"}

        def die_on_backup(src, dst, **kwargs):
            if Path(dst).parent.name == ".conflict-backup" and dying:
                dying.clear()
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr("nauro.store._atomic.os.replace", die_on_backup)

        merged, reporter = self._quarantining_pull(collision_store)

        assert merged == 0
        # No half-written evidence, and the run said something about it.
        assert entry_names(backup_dir) == set()
        assert reporter.warns

        merged, _reporter = self._quarantining_pull(collision_store)

        assert merged == 0
        backups = [path for path in backup_dir.iterdir()]
        assert len(backups) == 1
        assert backups[0].read_bytes() == decision_bytes(3, "Remote decision", "Chose B.")


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


class TestTrackedDecisionUpdates:
    """A decision's second sync and every one after it.

    The gate exists to stop a write from creating a duplicate number. A remote
    update to a decision the store already tracks, arriving at its own path,
    creates nothing: it is the ordinary pull that carries a rider or a
    supersession flip from one machine to another. Routing it through the
    classifier would be worse than pointless - the same-path leg resolves
    differing bytes by keeping the local copy, so a clean remote update would
    be dropped on the floor.
    """

    @pytest.fixture()
    def tracked(self, collision_store):
        rel = "decisions/003-shared.md"
        write_local_decision(collision_store, "003-shared.md", decision_bytes(3, "Shared", "v1."))
        track(collision_store, rel)
        return rel

    def test_a_remote_update_lands(self, collision_store, tracked):
        updated = decision_bytes(3, "Shared", "v2, with a rider from the other machine.")

        merged, reporter = pull(collision_store, [(tracked, updated)], etags={tracked: '"v2"'})

        assert merged == 1
        assert (collision_store / tracked).read_bytes() == updated
        assert load_state(collision_store).files[tracked].remote_etag == '"v2"'
        assert reporter.warns == []

    def test_a_two_sided_change_keeps_local_and_backs_up_remote(self, collision_store, tracked):
        local_edit = decision_bytes(3, "Shared", "v2 edited here.")
        (collision_store / tracked).write_bytes(local_edit)
        remote_edit = decision_bytes(3, "Shared", "v2 edited there.")

        merged, _reporter = pull(collision_store, [(tracked, remote_edit)], etags={tracked: '"v2"'})

        # The existing last-write-wins policy for decision files, unchanged.
        assert merged == 1
        assert (collision_store / tracked).read_bytes() == local_edit
        backups = [path.read_bytes() for path in (collision_store / ".conflict-backup").iterdir()]
        assert backups == [remote_edit]

    def test_the_barrier_admits_the_update_but_still_refuses_a_nested_path(
        self, collision_store, tracked
    ):
        state = load_state(collision_store)

        assert (
            pull_module._generic_write_allowed(
                collision_store,
                _Transfer(rel=tracked, etag='"v2"', _body=b""),
                state,
                _RecordingReporter(),
            )
            is True
        )
        for rel in ("decisions/sub/003-shared.md", "decisions/003-untracked.md"):
            assert (
                pull_module._generic_write_allowed(
                    collision_store,
                    _Transfer(rel=rel, etag='"v2"', _body=b""),
                    state,
                    _RecordingReporter(),
                )
                is False
            )

    def test_a_non_canonical_tracked_path_is_still_quarantined(self, collision_store):
        legacy = "decisions/004-old.MD"
        write_local_decision(collision_store, "004-old.MD", b"legacy\n")
        track(collision_store, legacy)

        merged, reporter = pull(collision_store, [(legacy, b"newer\n")], etags={legacy: '"v2"'})

        assert merged == 0
        assert (collision_store / "decisions/004-old.MD").read_bytes() == b"legacy\n"
        assert any("decisions/<name>.md" in warning for warning in reporter.warns)


class TestUnreadableLocalNames:
    """A local file the store's own readers cannot see still claims its number.

    ``list_decisions`` and the kernel's allocator match a lowercase ``.md``
    literally, so ``007-x.MD`` is invisible to them. If the corpus were blind to
    it too, a remote decision would land beside it and the allocator would go on
    to mint its number a third time.
    """

    def test_it_holds_back_a_remote_decision_claiming_its_number(self, collision_store):
        local = write_local_decision(collision_store, "007-local.MD", decision_bytes(7, "Local"))

        merged, reporter = pull(
            collision_store, [("decisions/007-remote.md", decision_bytes(7, "Remote"))]
        )

        assert merged == 0
        assert local.read_bytes() == decision_bytes(7, "Local")
        assert "007-remote.md" not in entry_names(collision_store / "decisions")
        assert any("lowercase '.md'" in warning for warning in reporter.warns)

    def test_it_is_a_case_variant_of_the_name_the_server_sends(self, collision_store):
        write_local_decision(collision_store, "007-remote.MD", decision_bytes(7, "Local"))

        merged, reporter = pull(
            collision_store, [("decisions/007-remote.md", decision_bytes(7, "Remote"))]
        )

        assert merged == 0
        assert "007-remote.md" not in entry_names(collision_store / "decisions")
        assert reporter.warns

    def test_an_unrelated_number_is_unaffected(self, collision_store):
        write_local_decision(collision_store, "007-local.MD", decision_bytes(7, "Local"))

        merged, _reporter = pull(
            collision_store, [("decisions/008-remote.md", decision_bytes(8, "Remote"))]
        )

        assert merged == 1
        assert "008-remote.md" in entry_names(collision_store / "decisions")


class TestLocallyDeletedFiles:
    """A tracked file the user deleted is not a conflict.

    A conflict exists to protect local content from being overwritten. There is
    no local content here, and the remote store is append-only, so the pull
    reinstalls the file. Treating the absence as a conflict made the resolver
    read a file that is not there, which aborted the whole run - including the
    state save for everything the same run had already written.
    """

    def test_a_deleted_tracked_file_is_reinstalled(self, collision_store):
        (collision_store / "stack.md").write_text("# Stack\n\nlocal\n")
        track(collision_store, "stack.md")
        (collision_store / "stack.md").unlink()

        merged, reporter = pull(
            collision_store,
            [("stack.md", b"# Stack\n\nremote\n")],
            etags={"stack.md": '"v2"'},
        )

        assert merged == 1
        assert (collision_store / "stack.md").read_bytes() == b"# Stack\n\nremote\n"
        state = load_state(collision_store)
        assert state.files["stack.md"].remote_etag == '"v2"'
        assert state.last_full_sync
        assert reporter.warns == []

    def test_a_deleted_tracked_decision_is_reinstalled(self, collision_store):
        rel = "decisions/003-shared.md"
        write_local_decision(collision_store, "003-shared.md", decision_bytes(3, "Shared", "v1."))
        track(collision_store, rel)
        (collision_store / rel).unlink()
        updated = decision_bytes(3, "Shared", "v2.")

        merged, reporter = pull(collision_store, [(rel, updated)], etags={rel: '"v2"'})

        assert merged == 1
        assert (collision_store / rel).read_bytes() == updated
        assert load_state(collision_store).files[rel].remote_etag == '"v2"'
        assert reporter.warns == []

    def test_the_rest_of_the_batch_still_lands_and_is_recorded(self, collision_store):
        (collision_store / "stack.md").write_text("# Stack\n\nlocal\n")
        track(collision_store, "stack.md")
        (collision_store / "stack.md").unlink()

        merged, _reporter = pull(
            collision_store,
            [
                ("decisions/009-remote.md", decision_bytes(9, "Remote")),
                ("stack.md", b"# Stack\n\nremote\n"),
            ],
            etags={"stack.md": '"v2"'},
        )

        assert merged == 2
        state = load_state(collision_store)
        assert "decisions/009-remote.md" in state.files
        assert "stack.md" in state.files

    def test_a_deleted_file_comes_back_when_the_remote_never_moved(self, collision_store):
        """Deleting a local file changes nothing on the server.

        The etag comparison ran first and matched, so the entry was dropped
        before anything looked at the disk - on that sync and on every sync
        after it. The remote store is the record for a file it holds, so a
        missing local copy asks for the file whatever the etag says.
        """
        (collision_store / "stack.md").write_text("# Stack\n\nshared\n")
        track(collision_store, "stack.md")
        (collision_store / "stack.md").unlink()

        merged, reporter = pull(
            collision_store,
            [("stack.md", b"# Stack\n\nshared\n")],
            etags={"stack.md": '"pushed"'},
        )

        assert merged == 1
        assert (collision_store / "stack.md").read_bytes() == b"# Stack\n\nshared\n"
        assert reporter.warns == []

    def test_a_deleted_decision_comes_back_when_the_remote_never_moved(self, collision_store):
        rel = "decisions/004-shared.md"
        body = decision_bytes(4, "Shared")
        write_local_decision(collision_store, "004-shared.md", body)
        track(collision_store, rel)
        (collision_store / rel).unlink()

        merged, reporter = pull(collision_store, [(rel, body)], etags={rel: '"pushed"'})

        assert merged == 1
        assert (collision_store / rel).read_bytes() == body
        assert reporter.warns == []

    def test_an_unchanged_entry_whose_local_copy_is_there_is_still_skipped(self, collision_store):
        """The shortcut still holds where it was right: no file, no fetch."""
        (collision_store / "stack.md").write_text("# Stack\n\nshared\n")
        track(collision_store, "stack.md")

        merged, reporter = pull(
            collision_store,
            [("stack.md", b"# Stack\n\nshared\n")],
            etags={"stack.md": '"pushed"'},
        )

        assert merged == 0
        assert reporter.infos == ["No remote changes"]

    def test_a_locally_modified_file_with_an_unchanged_remote_is_left_alone(self, collision_store):
        """An unmoved remote has nothing to say about a local edit.

        The shortcut is what protects work in progress. Moving the local-change
        probe above it would fetch the entry, call the edit a conflict, and
        rewrite or back up a file the server never changed.
        """
        (collision_store / "stack.md").write_text("# Stack\n\npushed\n")
        track(collision_store, "stack.md")
        recorded = load_state(collision_store).files["stack.md"].local_sha256
        (collision_store / "stack.md").write_text("# Stack\n\nlocal edit\n")

        merged, reporter = pull(
            collision_store,
            [("stack.md", b"# Stack\n\npushed\n")],
            etags={"stack.md": '"pushed"'},
        )

        assert merged == 0
        assert (collision_store / "stack.md").read_text() == "# Stack\n\nlocal edit\n"
        assert not (collision_store / CONFLICT_BACKUP_DIR).exists()
        assert load_state(collision_store).files["stack.md"].local_sha256 == recorded
        assert reporter.infos == ["No remote changes"]

    def test_a_deleted_decision_is_classified_before_it_is_reinstalled(self, collision_store):
        """Deleting a decision frees the number a local writer can then mint.

        Sync state still tracks the path, and a tracked canonical decision is
        exempt from the classifier as an ordinary same-path update. That holds
        only while the file is there: reinstalling it without a verdict would
        put two files on one number and push the duplicate.
        """
        rel = "decisions/004-foo.md"
        body = decision_bytes(4, "Foo")
        write_local_decision(collision_store, "004-foo.md", body)
        track(collision_store, rel)
        (collision_store / rel).unlink()
        # A local writer takes the freed number before the next sync.
        write_local_decision(collision_store, "004-bar.md", decision_bytes(4, "Bar", "Chose B."))

        merged, reporter = pull(collision_store, [(rel, body)], etags={rel: '"pushed"'})

        names = entry_names(collision_store / "decisions")
        assert merged == 1
        assert (collision_store / rel).read_bytes() == body
        assert "004-bar.md" not in names
        assert [name for name in names if name.startswith("004-")] == ["004-foo.md"]
        assert any(name.endswith("-bar.md") for name in names)
        assert any("Renumbered" in line for line in reporter.infos)


class TestUntrackedLocalFiles:
    """A file sync state never recorded still has bytes worth keeping.

    The local-change probe reads an untracked file as changed and the conflict
    probe read it as unchanged, so an entry with local content and a changed
    remote matched no worklist at all: nothing was written, nothing was warned
    about, and the remote version was never seen again.
    """

    def test_the_remote_lands_and_the_local_bytes_go_to_the_backup(self, collision_store):
        (collision_store / "stack.md").write_bytes(b"# Stack\n\nnever pushed\n")

        merged, _reporter = pull(collision_store, [("stack.md", b"# Stack\n\nremote\n")])

        assert merged == 1
        assert (collision_store / "stack.md").read_bytes() == b"# Stack\n\nremote\n"
        backups = list((collision_store / CONFLICT_BACKUP_DIR).iterdir())
        assert [path.read_bytes() for path in backups] == [b"# Stack\n\nnever pushed\n"]
        assert load_state(collision_store).files["stack.md"].remote_etag == '"stack.md-v1"'

    def test_an_append_only_file_merges_both_sides_instead(self, collision_store):
        (collision_store / "open-questions.md").write_text("## Open\n\n- local question\n")

        merged, _reporter = pull(
            collision_store, [("open-questions.md", b"## Open\n\n- remote question\n")]
        )

        assert merged == 1
        text = (collision_store / "open-questions.md").read_text()
        assert "- local question" in text
        assert "- remote question" in text

    def test_a_tracked_file_still_keeps_its_local_side(self, collision_store):
        """A published path's policy is unchanged: the local rewrite wins."""
        (collision_store / "stack.md").write_bytes(b"# Stack\n\npushed\n")
        track(collision_store, "stack.md")
        (collision_store / "stack.md").write_bytes(b"# Stack\n\nlocal rewrite\n")

        merged, _reporter = pull(
            collision_store,
            [("stack.md", b"# Stack\n\nremote rewrite\n")],
            etags={"stack.md": '"v2"'},
        )

        assert merged == 1
        assert (collision_store / "stack.md").read_bytes() == b"# Stack\n\nlocal rewrite\n"
        backups = list((collision_store / CONFLICT_BACKUP_DIR).iterdir())
        assert [path.read_bytes() for path in backups] == [b"# Stack\n\nremote rewrite\n"]


class TestUntrackedLocalFilesAlreadyLevel:
    """An untracked file the server already holds is adopted, not fought over.

    Absent sync state says nothing about a file's content, and the conflict
    route treats it as though it said the file changed. Where the manifest ETag
    is a content MD5 the run can settle the question outright, so it does: a
    file whose bytes are the server's bytes is recorded and left alone, with no
    fetch and no backup. Where the ETag is opaque nothing was compared, and the
    run keeps the conflict it would have had.
    """

    @staticmethod
    def _md5_etag(body: bytes) -> str:
        return f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'

    def _pull_counting_fetches(self, store, entries, *, etags=None):
        with patch.object(
            pull_module, "fetch_via_presigned_url", wraps=pull_module.fetch_via_presigned_url
        ) as fetch:
            report, reporter = pull_report(store, entries, etags=etags)
        return report, reporter, fetch.call_count

    def test_a_matching_untracked_file_is_adopted_without_a_fetch(self, collision_store):
        body = b"# Stack\n\nthe same bytes the server holds\n"
        (collision_store / "stack.md").write_bytes(body)

        report, reporter, fetches = self._pull_counting_fetches(
            collision_store, [("stack.md", body)], etags={"stack.md": self._md5_etag(body)}
        )

        assert fetches == 0
        assert report == PullReport()
        assert (collision_store / "stack.md").read_bytes() == body
        assert entry_names(collision_store / CONFLICT_BACKUP_DIR) == set()
        assert reporter.infos == ["Adopted 1 local file(s) already on the server"]
        # Recording it is what makes the next pull cheap: without the entry the
        # run would re-hash the same file forever.
        state = load_state(collision_store)
        assert state.files["stack.md"].remote_etag == self._md5_etag(body)
        assert state.files["stack.md"].local_sha256 == compute_sha256(collision_store / "stack.md")

    def test_a_differing_untracked_file_still_conflicts(self, collision_store):
        (collision_store / "stack.md").write_bytes(b"# Stack\n\nlocal only\n")
        remote = b"# Stack\n\nremote\n"

        report, _reporter, fetches = self._pull_counting_fetches(
            collision_store, [("stack.md", remote)], etags={"stack.md": self._md5_etag(remote)}
        )

        assert fetches == 1
        assert report.merged == 1
        assert (collision_store / "stack.md").read_bytes() == remote
        backups = list((collision_store / CONFLICT_BACKUP_DIR).iterdir())
        assert [path.read_bytes() for path in backups] == [b"# Stack\n\nlocal only\n"]

    def test_a_sibling_claiming_the_number_is_moved_before_the_adoption(self, collision_store):
        """The adoption leg stays below the decision gate, and this proves it.

        Adoption records a state entry, and a state entry on a decision is what
        marks its number as taken. Where a second local file claims that number,
        recording the entry ratifies the duplicate, and the store then pushes
        two files for one number. Only the classifier can see the sibling, so
        only the classifier may adopt a decision.

        Read from the top of ``_route_entry``, the ordering is what routes this
        entry to the gate rather than to the adoption leg, and the gate then
        moves the sibling out of the way before the record is adopted. Lift the
        adoption leg above the gate and this test fails on both assertions.
        """
        body = decision_bytes(42, "A ruling")
        write_local_decision(collision_store, "042-a-ruling.md", body)
        write_local_decision(collision_store, "042-other.md", decision_bytes(42, "Another ruling"))

        _report, reporter, _fetches = self._pull_counting_fetches(
            collision_store,
            [("decisions/042-a-ruling.md", body)],
            etags={"decisions/042-a-ruling.md": self._md5_etag(body)},
        )

        # The index file the writer keeps alongside the decisions claims no
        # number, so it is dropped rather than counted as a second nameless one.
        numbers = [
            number
            for number in (
                extract_decision_number(name)
                for name in entry_names(collision_store / DECISIONS_DIR)
            )
            if number is not None
        ]
        assert sorted(numbers) == [1, 42, 43]
        assert any(
            "Renumbered unpublished local decision 042 -> 043" in line for line in reporter.infos
        )

    def test_an_opaque_etag_never_skips_a_file(self, collision_store):
        """A multipart ETag answers nothing, and nothing is what it is read as.

        The local bytes here are the remote bytes, so a run that mistook an
        opaque ETag for a mismatch-free comparison would skip the file. One
        that mistakes it for evidence of anything at all would be guessing, and
        the conflict it would otherwise have had is the honest outcome.
        """
        body = b"# Stack\n\nthe same bytes the server holds\n"
        (collision_store / "stack.md").write_bytes(body)
        multipart = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}-2"'

        report, _reporter, fetches = self._pull_counting_fetches(
            collision_store, [("stack.md", body)], etags={"stack.md": multipart}
        )

        assert fetches == 1
        assert report.merged == 1
        assert entry_names(collision_store / CONFLICT_BACKUP_DIR) != set()


class TestLocalWriteFailures:
    """A local filesystem error stops one file, not the run.

    Everything a pull mutates happens under the decision lock, and the record
    of what landed is written at the end. An escaping OSError therefore lost
    that record for every file the run had already written, and the next run
    replayed into a store it no longer described.
    """

    @staticmethod
    def _refuse(monkeypatch, name: str, *, once: bool = False) -> None:
        """Fail the store write for one filename, as a full disk would."""
        real = pull_module.atomic_write_bytes
        pending = {name}

        def guarded(path, data):
            if path.name == name and (pending or not once):
                pending.clear()
                raise PermissionError(13, "Permission denied")
            return real(path, data)

        monkeypatch.setattr(pull_module, "atomic_write_bytes", guarded)

    def test_a_failed_write_is_reported_and_the_batch_continues(self, collision_store, monkeypatch):
        self._refuse(monkeypatch, "010-remote.md")

        merged, reporter = pull(
            collision_store,
            [
                ("decisions/010-remote.md", decision_bytes(10, "Refused")),
                ("decisions/011-remote.md", decision_bytes(11, "Fine")),
            ],
        )

        assert merged == 1
        state = load_state(collision_store)
        assert "decisions/011-remote.md" in state.files
        assert "decisions/010-remote.md" not in state.files
        assert any("010-remote.md" in warning for warning in reporter.warns)

    def test_the_next_run_retries_the_file_that_failed(self, collision_store, monkeypatch):
        self._refuse(monkeypatch, "010-remote.md", once=True)
        entries = [("decisions/010-remote.md", decision_bytes(10, "Refused"))]

        pull(collision_store, entries)
        merged, _reporter = pull(collision_store, entries)

        assert merged == 1
        assert (collision_store / "decisions/010-remote.md").exists()
        assert "decisions/010-remote.md" in load_state(collision_store).files

    def test_a_failed_generic_write_keeps_the_state_of_what_landed(
        self, collision_store, monkeypatch
    ):
        (collision_store / "stack.md").write_text("# Stack\n\nlocal\n")
        track(collision_store, "stack.md")
        self._refuse(monkeypatch, "stack.md")

        merged, reporter = pull(
            collision_store,
            [
                ("stack.md", b"# Stack\n\nremote\n"),
                ("decisions/012-remote.md", decision_bytes(12, "Fine")),
            ],
            etags={"stack.md": '"v2"'},
        )

        assert merged == 1
        state = load_state(collision_store)
        assert "decisions/012-remote.md" in state.files
        assert state.files["stack.md"].remote_etag != '"v2"'
        assert any("stack.md" in warning for warning in reporter.warns)

    def test_a_failing_completion_pass_does_not_abort_the_run(self, collision_store, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(pull_module, "run_completion_pass", refuse)

        merged, reporter = pull(
            collision_store, [("decisions/013-remote.md", decision_bytes(13, "Remote"))]
        )

        assert merged == 1
        assert "decisions/013-remote.md" in load_state(collision_store).files
        assert any("decisions:" in warning for warning in reporter.warns)

    def test_a_refused_file_holds_back_the_full_sync_timestamp(self, collision_store, monkeypatch):
        self._refuse(monkeypatch, "014-remote.md")

        _merged, reporter = pull(
            collision_store, [("decisions/014-remote.md", decision_bytes(14, "Refused"))]
        )

        # The store was not fully synced: one file is still owed.
        assert load_state(collision_store).last_full_sync == ""
        # And the run says so rather than signing off as a quiet no-op.
        assert "No remote changes" not in reporter.infos
        assert any("next sync" in line for line in reporter.infos)

    def test_a_failed_transfer_holds_back_the_full_sync_timestamp(
        self, collision_store, monkeypatch
    ):
        """A fetch that never arrived is a file the next sync still owes.

        Only a local write error was counted, so a transport failure left the
        store short of the server while the stamp said the two agreed.
        """

        def refuse(_url, **_kwargs):
            raise PresignError("connection reset by peer")

        monkeypatch.setattr(pull_module, "fetch_via_presigned_url", refuse)

        merged, reporter = pull(collision_store, [("stack.md", b"# Stack\n\nremote\n")])

        assert merged == 0
        assert load_state(collision_store).last_full_sync == ""
        assert any("next sync" in line for line in reporter.infos)

    def test_a_presign_shortfall_holds_back_the_full_sync_timestamp(
        self, collision_store, monkeypatch
    ):
        """A URL the server never minted leaves its file unfetched."""
        monkeypatch.setattr(pull_module, "request_presigned_urls", lambda *_args, **_kwargs: [])

        merged, reporter = pull(
            collision_store, [("decisions/016-remote.md", decision_bytes(16, "Unfetched"))]
        )

        assert merged == 0
        assert not (collision_store / "decisions/016-remote.md").exists()
        assert load_state(collision_store).last_full_sync == ""
        assert any("next sync" in line for line in reporter.infos)

    def test_a_quarantine_does_not_hold_back_the_full_sync_timestamp(self, collision_store):
        """A collision no rerun resolves must not freeze the stamp for good.

        The quarantined decision is reported on its own and surfaced by
        ``nauro sync --status`` until a person settles the number. Holding the
        stamp until then would say nothing about the rest of the store, which
        this run did write in full.
        """
        write_local_decision(collision_store, "003-local.md", decision_bytes(3, "Local decision"))
        track(collision_store, "decisions/003-local.md")

        report, reporter = pull_report(
            collision_store,
            [("decisions/003-remote.md", decision_bytes(3, "Remote decision", "Chose B."))],
        )

        assert report.merged == 0
        assert report.refused == 0
        assert report.skipped_permanent == 1
        assert load_state(collision_store).last_full_sync
        assert any("cannot install" in line for line in reporter.infos)

    def test_a_lock_timeout_leaves_an_unreadable_state_file_alone(
        self, collision_store, monkeypatch
    ):
        """A run that never reached the write phase does not rewrite state.

        An unreadable state file loads as an empty one, so a save on the way
        out of a run that wrote nothing would untrack the whole store and hand
        the corruption a permanent form.
        """
        corrupt = b'{"files": {"stack.md": '
        (collision_store / ".sync-state.json").write_bytes(corrupt)

        def refuse_lock(store_path, timeout, *_args, **_kwargs):
            raise SyncLockTimeoutError(store_path, timeout, "decision")

        monkeypatch.setattr(pull_module, "decision_lock", refuse_lock)

        with pytest.raises(SyncLockTimeoutError):
            pull(collision_store, [("decisions/015-remote.md", decision_bytes(15, "Remote"))])

        assert (collision_store / ".sync-state.json").read_bytes() == corrupt


class TestServerOutOfReach:
    """A run that fetched nothing must not read as a store already in step.

    Both legs returned an empty report, which counts zero refusals and says
    zero files are owed. The command then exited 0 and pushed over a pull that
    never happened.
    """

    def test_a_manifest_failure_says_the_run_never_saw_the_server(
        self, collision_store, monkeypatch
    ):
        def refuse(_project_id, **_kwargs):
            raise PresignError("503 service unavailable")

        monkeypatch.setattr(pull_module, "fetch_manifest", refuse)
        reporter = _RecordingReporter()

        report = run_pull(CLOUD_PID, collision_store, reporter)

        assert report.manifest_read is False
        assert report.left_work_behind is True
        assert load_state(collision_store).last_full_sync == ""
        assert any("manifest fetch failed" in warning for warning in reporter.warns)

    def test_an_auth_failure_on_the_manifest_reads_the_same_way(self, collision_store, monkeypatch):
        def refuse(_project_id, **_kwargs):
            raise AuthRefreshError("session expired; run 'nauro auth login'")

        monkeypatch.setattr(pull_module, "fetch_manifest", refuse)

        report = run_pull(CLOUD_PID, collision_store, _RecordingReporter())

        assert report.manifest_read is False
        assert report.left_work_behind is True

    def test_a_total_presign_failure_counts_every_planned_file(self, collision_store, monkeypatch):
        """Here the run does know what it missed, so it says how much."""

        def refuse(*_args, **_kwargs):
            raise PresignError("403 forbidden")

        monkeypatch.setattr(pull_module, "request_presigned_urls", refuse)

        report, reporter = pull_report(
            collision_store,
            [
                ("decisions/018-remote.md", decision_bytes(18, "Unfetched")),
                ("stack.md", b"# Stack\n\nremote\n"),
            ],
        )

        assert report.refused == 2
        assert report.merged == 0
        assert report.manifest_read is True
        assert report.left_work_behind is True
        assert not (collision_store / "decisions/018-remote.md").exists()
        assert load_state(collision_store).last_full_sync == ""
        assert any("presign request failed" in warning for warning in reporter.warns)
        assert any("Left 2 item(s) for the next sync" in line for line in reporter.infos)

    def test_a_clean_run_says_it_read_the_server(self, collision_store):
        report, _reporter = pull_report(collision_store, [])

        assert report.manifest_read is True
        assert report.left_work_behind is False


class TestPartialWrites:
    """A write that dies halfway must not leave a shortened file behind.

    A plain write truncates the target first, so an error partway through
    leaves bytes that are neither version. Nothing downstream can tell: the
    next push reads the file as a local change and uploads the truncation over
    the good remote copy, and there is no backup of what it replaced.
    """

    def test_a_dead_write_leaves_the_original_file_and_its_state_entry(
        self, collision_store, monkeypatch
    ):
        original = b"# Stack\n\nthe local version, in full\n"
        (collision_store / "stack.md").write_bytes(original)
        track(collision_store, "stack.md")
        before = load_state(collision_store).files["stack.md"]

        real_replace = os.replace

        def die_on_stack(src, dst, **kwargs):
            if Path(dst).name == "stack.md":
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr("nauro.store._atomic.os.replace", die_on_stack)

        merged, reporter = pull(
            collision_store,
            [("stack.md", b"# Stack\n\nthe remote version\n")],
            etags={"stack.md": '"v2"'},
        )

        assert merged == 0
        assert (collision_store / "stack.md").read_bytes() == original
        after = load_state(collision_store).files["stack.md"]
        assert (after.local_sha256, after.remote_etag) == (before.local_sha256, before.remote_etag)
        assert any("stack.md" in warning for warning in reporter.warns)
        # The half-written sibling is not left lying in the store either.
        assert not list(collision_store.glob(".stack.md.*.tmp"))

    def test_a_dead_decision_install_lands_nothing_and_retries(self, collision_store, monkeypatch):
        """The install leg has the worst chain if a truncation survives.

        A shortened decision file is untracked, so the next run classifies it
        as a local record, keeps it over the remote one, and pushes it - the
        store's own copy of that decision, silently replaced by a fragment.
        """
        rel = "decisions/016-remote.md"
        body = decision_bytes(16, "Remote")
        real_replace = os.replace
        dying = {rel}

        def die_on_install(src, dst, **kwargs):
            if Path(dst).name == "016-remote.md" and dying:
                dying.clear()
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr("nauro.store._atomic.os.replace", die_on_install)

        merged, reporter = pull(collision_store, [(rel, body)])

        assert merged == 0
        assert "016-remote.md" not in entry_names(collision_store / "decisions")
        assert rel not in load_state(collision_store).files
        assert not list((collision_store / "decisions").glob(".016-remote.md.*.tmp"))
        assert any("016-remote.md" in warning for warning in reporter.warns)

        # Nothing about the store now argues against the remote decision, so
        # the next run installs it whole.
        merged, _reporter = pull(collision_store, [(rel, body)])

        assert merged == 1
        assert (collision_store / rel).read_bytes() == body
        assert rel in load_state(collision_store).files


class TestStrandedTmpSiblings:
    """A kill between an atomic write and its rename strands a full copy.

    Nothing reads the file and both sync directions exclude it, so no command
    counted or removed one and a store kept them for its whole life. The pull
    sweeps them because it already holds the sync lock and already walks the
    store.
    """

    @staticmethod
    def _strand(path: Path, content: bytes, *, age_seconds: float) -> Path:
        """Leave a tmp sibling of ``path`` behind, as a killed write would."""
        with patch.object(_atomic.os, "replace", lambda src, dst: None):
            atomic_write_bytes(path, content)
        orphan = next(entry for entry in path.parent.iterdir() if is_tmp_sibling(entry.name))
        stranded_at = time.time() - age_seconds
        os.utime(orphan, (stranded_at, stranded_at))
        return orphan

    def test_old_siblings_anywhere_in_the_store_are_swept_and_reported(self, collision_store):
        backup_dir = collision_store / CONFLICT_BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        in_backups = self._strand(backup_dir / "losing-side.md", b"a backup\n", age_seconds=600)
        in_store = self._strand(collision_store / "stack.md", b"# Stack\n", age_seconds=600)

        _merged, reporter = pull(collision_store, [])

        assert not in_backups.exists()
        assert not in_store.exists()
        assert "Cleaned 2 interrupted write(s)" in reporter.infos

    def test_a_fresh_sibling_is_left_alone(self, collision_store):
        """The age floor is what keeps the sweep off a write in progress."""
        live = self._strand(collision_store / "stack.md", b"# Stack\n", age_seconds=0)

        _merged, reporter = pull(collision_store, [])

        assert live.exists()
        assert not any("interrupted write" in line for line in reporter.infos)

    def test_a_sibling_it_cannot_remove_warns_and_the_pull_carries_on(
        self, collision_store, monkeypatch
    ):
        stranded = self._strand(collision_store / "stack.md", b"# Stack\n", age_seconds=600)
        real_unlink = Path.unlink

        def refuse(self, *args, **kwargs):
            if is_tmp_sibling(self.name):
                raise PermissionError(13, "Permission denied")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(pull_module.Path, "unlink", refuse)

        merged, reporter = pull(
            collision_store, [("decisions/017-remote.md", decision_bytes(17, "Remote"))]
        )

        assert merged == 1
        assert stranded.exists()
        assert any("interrupted write" in warning for warning in reporter.warns)

    def test_a_walk_that_breaks_partway_still_lets_the_pull_run(self, collision_store, monkeypatch):
        """The sweep is best-effort hygiene, not a gate on the sync.

        It runs before the manifest fetch, so an error escaping the traversal
        aborts a pull that never started, and the session-start hook swallows
        that into a silently skipped sync. A directory can vanish underneath
        the walk at any time: the store is one other processes write into.
        """
        stranded = self._strand(collision_store / "stack.md", b"# Stack\n", age_seconds=600)
        real_rglob = Path.rglob

        def break_partway(self, *args, **kwargs):
            if self != collision_store:
                yield from real_rglob(self, *args, **kwargs)
                return
            yield stranded
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(pull_module.Path, "rglob", break_partway)

        merged, reporter = pull(
            collision_store, [("decisions/020-remote.md", decision_bytes(20, "Remote"))]
        )

        assert merged == 1
        assert (collision_store / "decisions/020-remote.md").exists()
        # What the walk reached before it broke was still cleaned up.
        assert not stranded.exists()
        assert any("could not finish looking" in warning for warning in reporter.warns)
