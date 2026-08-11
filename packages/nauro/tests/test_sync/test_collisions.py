"""Tests for the decision-number collision classifier and its crash windows.

The renumber sequence is rename, heading rewrite, hash-index update, install,
state save. Every window between two of those steps is simulated here by
building the on-disk state a crash would leave and asserting the next pull
recovers it without minting a second number.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from nauro_core import parse_decision

from nauro.constants import DECISION_HASHES_FILE
from nauro.sync import corpus as corpus_module
from nauro.sync.collisions import (
    DecisionOutcome,
    DestinationOccupiedError,
    QuarantineReason,
    apply_renumber,
    classify_decision,
    is_canonical_decision_path,
    run_completion_pass,
)
from nauro.sync.corpus import DecisionCorpus
from nauro.sync.headings import (
    has_rewritable_heading,
    heading_number,
    rewrite_decision_heading,
    strip_number_prefix,
)
from nauro.sync.quarantine import (
    _parse_backup_name,
    backup_name,
    list_quarantine_backups,
    path_key,
    save_quarantine_backup,
    unresolved_quarantines,
)
from nauro.sync.state import FileState, SyncState, load_state, save_state
from tests.test_sync.conftest import (
    _STAMPED_DECISION,
    _STAMPED_SHA,
    _scaffolded_cloud_project,
    _seed_token,
    decision_bytes,
    pull,
    write_local_decision,
)


@pytest.fixture()
def store(tmp_path):
    project_store = _scaffolded_cloud_project("collisionunit", tmp_path)
    _seed_token()
    return project_store


def _write_hash_index(store, entries: dict[str, str]) -> None:
    (store / DECISION_HASHES_FILE).write_text(
        json.dumps(
            {
                content_hash: {"decision_id": stem, "timestamp": "2026-08-10T00:00:00Z"}
                for content_hash, stem in entries.items()
            },
            indent=2,
        )
        + "\n"
    )


def _read_hash_index(store) -> dict:
    return json.loads((store / DECISION_HASHES_FILE).read_text())


def _decision_names(store) -> list[str]:
    return sorted(path.name for path in (store / "decisions").glob("*.md"))


# --- heading and filename primitives ---------------------------------------


class TestHeadingPrimitives:
    def test_strip_number_prefix_drops_leading_run_and_hyphen(self):
        assert strip_number_prefix("003-local-decision.md") == "local-decision.md"

    def test_strip_number_prefix_keeps_digits_without_a_hyphen(self):
        assert strip_number_prefix("003local.md") == "003local.md"

    @pytest.mark.parametrize("separator", ["—", "-"])
    def test_heading_number_reads_both_separators(self, separator):
        assert heading_number(f"# 008 {separator} Title\n") == 8

    def test_heading_number_reads_the_lax_forms_the_parser_accepts(self):
        # As lax as parse_decision, so a filename/heading mismatch is detected
        # even on a heading this layer refuses to rewrite.
        assert heading_number("#  008 — Title\n") == 8
        assert heading_number("# 8 — Title\n") == 8
        assert heading_number("## 008 — Title\n") is None
        assert heading_number("#Not a heading\n") is None

    def test_rewritable_is_narrower_than_readable(self):
        assert has_rewritable_heading("# 008 — Title\n", 8) is True
        assert has_rewritable_heading("#  008 — Title\n", 8) is False
        assert has_rewritable_heading("# 8 — Title\n", 8) is False

    def test_rewrite_preserves_the_separator_and_the_rest_of_the_line(self):
        assert rewrite_decision_heading("# 008 - Remote\n\nBody\n", 8, 9) == (
            "# 009 - Remote\n\nBody\n"
        )

    def test_rewrite_touches_only_the_heading(self):
        stamped = _STAMPED_DECISION.decode("utf-8")

        rewritten = rewrite_decision_heading(stamped, 99, 100)

        assert rewritten == stamped.replace("# 099 ", "# 100 ")
        assert f"base_commit: {_STAMPED_SHA}" in rewritten


# --- classification --------------------------------------------------------


class TestClassify:
    def _classify(self, store, remote_content, *, state=None, manifest=frozenset()):
        return classify_decision(
            DecisionCorpus.scan(store),
            "decisions/003-remote.md",
            remote_content,
            state or SyncState(),
            manifest,
        )

    def test_no_collider_installs(self, store):
        verdict = self._classify(store, decision_bytes(3, "Remote"))
        assert verdict.outcome is DecisionOutcome.install

    def test_identical_bytes_canonicalize(self, store):
        body = decision_bytes(3, "Shared")
        write_local_decision(store, "003-local.md", body)
        verdict = self._classify(store, body)
        assert verdict.outcome is DecisionOutcome.canonicalize
        assert verdict.collider.name == "003-local.md"

    def test_isolated_difference_renumbers_local(self, store):
        write_local_decision(store, "003-local.md", decision_bytes(3, "Local"))
        verdict = self._classify(store, decision_bytes(3, "Remote", "Chose B."))
        assert verdict.outcome is DecisionOutcome.renumber_local

    def test_tracked_collider_outranks_identical_bytes(self, store):
        body = decision_bytes(3, "Shared")
        write_local_decision(store, "003-local.md", body)
        state = SyncState()
        state.files["decisions/003-local.md"] = FileState(local_sha256="x", remote_etag='"y"')
        verdict = self._classify(store, body, state=state)
        assert verdict.outcome is DecisionOutcome.quarantine
        assert verdict.reason is QuarantineReason.tracked_locally

    def test_manifest_membership_outranks_identical_bytes(self, store):
        body = decision_bytes(3, "Shared")
        write_local_decision(store, "003-local.md", body)
        verdict = self._classify(store, body, manifest=frozenset({"decisions/003-local.md"}))
        assert verdict.outcome is DecisionOutcome.quarantine
        assert verdict.reason is QuarantineReason.published_remotely

    def test_backref_from_the_collider_quarantines(self, store):
        write_local_decision(
            store,
            "003-local.md",
            decision_bytes(3, "Local", superseded_by="4", status="superseded"),
        )
        verdict = self._classify(store, decision_bytes(3, "Remote", "Chose B."))
        assert verdict.reason is QuarantineReason.referenced

    def test_unrelated_unparseable_file_blocks_verification(self, store):
        write_local_decision(store, "003-local.md", decision_bytes(3, "Local"))
        write_local_decision(store, "009-broken.md", b"not a decision\n")
        verdict = self._classify(store, decision_bytes(3, "Remote", "Chose B."))
        assert verdict.reason is QuarantineReason.store_unverifiable


# --- crash windows ---------------------------------------------------------


class TestRenumberCrashWindows:
    """Each test builds the store a crash would leave, then pulls again."""

    def test_rename_landed_but_heading_did_not(self, store):
        # Window between the rename and the heading rewrite.
        write_local_decision(store, "004-local.md", decision_bytes(3, "Local decision"))
        _write_hash_index(store, {"hash-a": "003-local"})
        remote = decision_bytes(3, "Remote decision", "Chose B.")

        merged, reporter = pull(store, [("decisions/003-remote.md", remote)])

        assert merged == 1
        assert _decision_names(store) == [
            "001-initial-setup.md",
            "003-remote.md",
            "004-local.md",
        ]
        assert (store / "decisions/004-local.md").read_bytes() == decision_bytes(
            4, "Local decision"
        )
        assert _read_hash_index(store)["hash-a"]["decision_id"] == "004-local"
        assert reporter.warns == []
        assert any("Completed an interrupted renumber" in info for info in reporter.infos)

    def test_heading_landed_but_hash_index_did_not(self, store):
        # Window between the heading rewrite and the hash-index update.
        write_local_decision(store, "004-local.md", decision_bytes(4, "Local decision"))
        _write_hash_index(store, {"hash-a": "003-local"})
        remote = decision_bytes(3, "Remote decision", "Chose B.")

        merged, reporter = pull(store, [("decisions/003-remote.md", remote)])

        assert merged == 1
        assert _read_hash_index(store)["hash-a"]["decision_id"] == "004-local"
        assert reporter.warns == []

    def test_install_landed_but_state_save_did_not(self, store):
        # Window between installing the remote file and saving sync state.
        remote = decision_bytes(3, "Remote decision", "Chose B.")
        write_local_decision(store, "004-local.md", decision_bytes(4, "Local decision"))
        write_local_decision(store, "003-remote.md", remote)

        merged, reporter = pull(store, [("decisions/003-remote.md", remote)])

        assert merged == 0
        assert _decision_names(store) == [
            "001-initial-setup.md",
            "003-remote.md",
            "004-local.md",
        ]
        assert "decisions/003-remote.md" in load_state(store).files
        assert reporter.warns == []

    def test_heading_rewrite_failure_quarantines_without_mutating(self, store, monkeypatch):
        """The rewrite is verified before the rename, so a rewrite that would
        not land leaves the store exactly as it was."""
        body = decision_bytes(3, "Local decision")
        write_local_decision(store, "003-local.md", body)
        monkeypatch.setattr(
            "nauro.sync.collisions.rewrite_decision_heading",
            lambda text, old_num, new_num: text,
        )

        merged, reporter = pull(
            store, [("decisions/003-remote.md", decision_bytes(3, "Remote", "Chose B."))]
        )

        assert merged == 0
        assert not (store / "decisions/003-remote.md").exists()
        assert _decision_names(store) == ["001-initial-setup.md", "003-local.md"]
        assert (store / "decisions/003-local.md").read_bytes() == body
        assert any("could not be rewritten" in warning for warning in reporter.warns)


class TestCompletionPass:
    def test_clean_store_is_untouched_and_reports_nothing(self, store):
        write_local_decision(store, "004-local.md", decision_bytes(4, "Local"))
        before = (store / "decisions/004-local.md").read_bytes()

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome == run_completion_pass(DecisionCorpus.scan(store))
        assert outcome.repaired == ()
        assert outcome.unrepairable == ()
        assert outcome.retargeted_stems == ()
        assert (store / "decisions/004-local.md").read_bytes() == before

    def test_repair_is_idempotent(self, store):
        path = write_local_decision(store, "004-local.md", decision_bytes(3, "Local"))

        first = run_completion_pass(DecisionCorpus.scan(store))
        repaired_bytes = path.read_bytes()
        second = run_completion_pass(DecisionCorpus.scan(store))

        assert [(r.from_num, r.to_num) for r in first.repaired] == [(3, 4)]
        assert second.repaired == ()
        assert path.read_bytes() == repaired_bytes == decision_bytes(4, "Local")

    def test_referenced_mismatch_is_reported_not_repaired(self, store):
        path = write_local_decision(store, "004-local.md", decision_bytes(3, "Local"))
        write_local_decision(store, "005-child.md", decision_bytes(5, "Child", supersedes="4"))
        before = path.read_bytes()

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome.repaired == ()
        assert outcome.unrepairable == (path,)
        assert path.read_bytes() == before

    def test_contested_filename_number_is_reported_not_repaired(self, store):
        path = write_local_decision(store, "004-local.md", decision_bytes(3, "Local"))
        write_local_decision(store, "004-other.md", decision_bytes(4, "Other"))
        before = path.read_bytes()

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome.unrepairable == (path,)
        assert path.read_bytes() == before

    def test_dangling_stem_with_two_slug_matches_is_left_alone(self, store):
        write_local_decision(store, "004-local.md", decision_bytes(4, "Local"))
        write_local_decision(store, "005-local.md", decision_bytes(5, "Local"))
        _write_hash_index(store, {"hash-a": "003-local"})

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome.retargeted_stems == ()
        assert _read_hash_index(store)["hash-a"]["decision_id"] == "003-local"

    def test_dangling_stem_for_a_deleted_decision_is_left_alone(self, store):
        _write_hash_index(store, {"hash-a": "003-deleted-outright"})

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome.retargeted_stems == ()
        assert _read_hash_index(store)["hash-a"]["decision_id"] == "003-deleted-outright"

    def test_unrelated_hash_index_keys_survive_a_retarget(self, store):
        write_local_decision(store, "004-local.md", decision_bytes(4, "Local"))
        (store / DECISION_HASHES_FILE).write_text(
            json.dumps(
                {
                    "hash-a": {
                        "decision_id": "003-local",
                        "timestamp": "2026-08-10T00:00:00Z",
                        "extra": "kept",
                    }
                },
                indent=2,
            )
            + "\n"
        )

        run_completion_pass(DecisionCorpus.scan(store))

        entry = _read_hash_index(store)["hash-a"]
        assert entry == {
            "decision_id": "004-local",
            "timestamp": "2026-08-10T00:00:00Z",
            "extra": "kept",
        }


class TestNonCanonicalHeadingNumbers:
    """The decision parser accepts headings the rewriter must not touch.

    ``parse_decision`` reads any digit run, so an unpadded ``# 42`` or an
    over-padded ``# 0042`` heading yields a perfectly valid, isolated,
    unpublished collider. The rewriter only addresses the canonical
    zero-padded form, so those files quarantine instead of renumbering -
    and, critically, nothing on disk moves first.
    """

    @pytest.mark.parametrize("heading", ["# 42", "# 0042"])
    def test_collider_quarantines_with_nothing_mutated(self, store, heading):
        body = decision_bytes(42, "Local decision").replace(b"\n# 042", heading.encode())
        write_local_decision(store, "042-local.md", body)
        remote = decision_bytes(42, "Remote decision", "Chose B.")

        merged, reporter = pull(store, [("decisions/042-remote.md", remote)])

        assert merged == 0
        assert _decision_names(store) == ["001-initial-setup.md", "042-local.md"]
        assert (store / "decisions/042-local.md").read_bytes() == body
        assert not (store / "decisions/042-remote.md").exists()
        assert "decisions/042-remote.md" not in load_state(store).files
        assert any("canonical form" in warning for warning in reporter.warns)

    @pytest.mark.parametrize("heading", ["# 42", "# 0042"])
    def test_classifier_names_the_heading_as_the_reason(self, store, heading):
        body = decision_bytes(42, "Local decision").replace(b"\n# 042", heading.encode())
        write_local_decision(store, "042-local.md", body)

        verdict = classify_decision(
            DecisionCorpus.scan(store),
            "decisions/042-remote.md",
            decision_bytes(42, "Remote", "Chose B."),
            SyncState(),
            frozenset(),
        )

        assert verdict.outcome is DecisionOutcome.quarantine
        assert verdict.reason is QuarantineReason.heading_not_rewritable

    @pytest.mark.parametrize("heading", ["# 42", "# 0042"])
    def test_completion_pass_reports_a_quarantine_not_an_unrepairable(self, store, heading):
        """A filename/heading mismatch the rewriter cannot address is reported
        as a quarantine with a human fix, identically on every run."""
        body = decision_bytes(42, "Local decision").replace(b"\n# 042", heading.encode())
        path = write_local_decision(store, "043-local.md", body)

        first = run_completion_pass(DecisionCorpus.scan(store))
        second = run_completion_pass(DecisionCorpus.scan(store))

        assert first.quarantined == (path,)
        assert first.unrepairable == ()
        assert first.repaired == ()
        assert second == first
        assert path.read_bytes() == body

    def test_pull_reports_the_quarantined_mismatch_every_run(self, store):
        body = decision_bytes(42, "Local decision").replace(b"\n# 042", b"\n# 42")
        write_local_decision(store, "043-local.md", body)

        for _ in range(2):
            merged, reporter = pull(store, [])
            assert merged == 0
            assert any("canonical form" in warning for warning in reporter.warns)
        assert (store / "decisions/043-local.md").read_bytes() == body


class TestCorpusIsReadOnce:
    """Freshness per verdict must not mean a full reparse per verdict."""

    @staticmethod
    def _seed(store, count: int) -> None:
        for num in range(3, 3 + count):
            write_local_decision(store, f"{num:03d}-local.md", decision_bytes(num, f"Local {num}"))

    def test_no_decision_file_is_parsed_twice_in_one_pull(self, store, monkeypatch):
        collisions = 6
        self._seed(store, collisions)

        parses: Counter[str] = Counter()
        real_parse = corpus_module.parse_decision

        def counting_parse(text, filename):
            parses[filename] += 1
            return real_parse(text, filename)

        monkeypatch.setattr(corpus_module, "parse_decision", counting_parse)

        entries = [
            (f"decisions/{num:03d}-remote.md", decision_bytes(num, f"Remote {num}", "Chose B."))
            for num in range(3, 3 + collisions)
        ]
        merged, reporter = pull(store, entries)

        assert merged == collisions
        assert reporter.warns == []
        # The invariant, not an aggregate: one parse per file per run, however
        # many verdicts consulted the reference index.
        assert parses and max(parses.values()) == 1

    def test_a_pull_scans_the_decisions_directory_exactly_twice(self, store, monkeypatch):
        self._seed(store, 3)

        scans: list[str] = []
        real_scan = DecisionCorpus.scan

        def counting_scan(store_path):
            scans.append(str(store_path))
            return real_scan(store_path)

        monkeypatch.setattr(DecisionCorpus, "scan", staticmethod(counting_scan))

        merged, _reporter = pull(
            store, [("decisions/003-remote.md", decision_bytes(3, "Remote", "Chose B."))]
        )

        assert merged == 1
        # One scan while planning, outside the decision lock, to order the
        # worklist; one inside it, which is the authoritative view every
        # verdict is taken against and the writer keeps current from there.
        assert len(scans) == 2

    def test_a_pull_with_nothing_to_heal_reads_no_decision_bodies(self, store, monkeypatch):
        self._seed(store, 4)

        reads: list[str] = []
        real_read = corpus_module.read_text_lenient

        def counting_read(path):
            reads.append(path.name)
            return real_read(path)

        monkeypatch.setattr(corpus_module, "read_text_lenient", counting_read)

        merged, reporter = pull(store, [])

        assert merged == 0
        assert reporter.warns == []
        # The completion pass reads headings line by line, never whole bodies.
        assert reads == []


class TestIrregularHoldersContestANumber:
    """An entry Nauro never reads is still a claim on the number.

    The completion pass repairs a heading only while the number it repairs
    toward is uncontested. An entry with no readable content is exactly the
    ambiguity that must stop it, not a claim to overlook for lack of content.
    """

    def test_a_directory_on_the_number_stops_a_heading_repair(self, store):
        moved = write_local_decision(store, "005-moved.md", decision_bytes(3, "Moved"))
        (store / "decisions" / "005-blocker.md").mkdir()
        before = moved.read_bytes()

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome.repaired == ()
        assert outcome.unrepairable == (moved,)
        assert moved.read_bytes() == before

    def test_a_symlink_on_the_number_stops_a_heading_repair(self, store):
        target = write_local_decision(store, "009-target.md", decision_bytes(9, "Target"))
        moved = write_local_decision(store, "005-moved.md", decision_bytes(3, "Moved"))
        (store / "decisions" / "005-blocker.md").symlink_to(target)
        before = moved.read_bytes()

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert outcome.repaired == ()
        assert outcome.unrepairable == (moved,)
        assert moved.read_bytes() == before

    def test_an_irregular_entry_on_another_number_does_not_stop_the_repair(self, store):
        moved = write_local_decision(store, "005-moved.md", decision_bytes(3, "Moved"))
        (store / "decisions" / "007-blocker.md").mkdir()

        outcome = run_completion_pass(DecisionCorpus.scan(store))

        assert [(repair.from_num, repair.to_num) for repair in outcome.repaired] == [(3, 5)]
        assert outcome.unrepairable == ()
        assert moved.read_bytes() == decision_bytes(5, "Moved")


class TestRenumberRefusalReasons:
    def test_a_taken_destination_name_is_not_blamed_on_the_heading(self, store, monkeypatch):
        write_local_decision(store, "003-local.md", decision_bytes(3, "Local"))
        write_local_decision(store, "001-local.md", decision_bytes(1, "Taken"))
        # Force the mint onto a name that is already on disk.
        monkeypatch.setattr(
            DecisionCorpus, "claimed_numbers", lambda self: frozenset({0}), raising=True
        )
        corpus = DecisionCorpus.scan(store)

        with pytest.raises(DestinationOccupiedError) as excinfo:
            apply_renumber(corpus, store / "decisions/003-local.md", 3, frozenset())

        assert excinfo.value.reason is QuarantineReason.destination_occupied
        assert (store / "decisions/003-local.md").exists()


class TestReservedDeviceNames:
    """Windows resolves a device basename whatever the extension, and the
    superscript digits are the same devices as the ASCII ones."""

    @pytest.mark.parametrize(
        "filename",
        [
            "com1.md",
            "com¹.md",
            "com².md",
            "com³.md",
            "lpt¹.md",
            "lpt².md",
            "lpt³.md",
            "con.md",
            "nul.md",
        ],
    )
    def test_a_device_name_is_not_canonical(self, filename):
        assert is_canonical_decision_path(f"decisions/{filename}") is False

    @pytest.mark.parametrize("filename", ["240-com¹x.md", "240-con.md", "001-initial-setup.md"])
    def test_a_name_merely_containing_one_is_fine(self, filename):
        assert is_canonical_decision_path(f"decisions/{filename}") is True

    def test_the_explicit_set_agrees_with_windows(self):
        """Pinned against the platform rule this stands in for, since
        ``PureWindowsPath.is_reserved`` is deprecated from 3.13 and this
        package supports 3.10 upward."""
        from pathlib import PureWindowsPath

        for stem in ("com1", "com¹", "com²", "com³", "lpt¹", "con", "nul"):
            assert PureWindowsPath(f"{stem}.md").is_reserved() is True
            assert is_canonical_decision_path(f"decisions/{stem}.md") is False
        for stem in ("240-fine", "240-com¹x"):
            assert PureWindowsPath(f"{stem}.md").is_reserved() is False
            assert is_canonical_decision_path(f"decisions/{stem}.md") is True


class TestBackupFilenames:
    @pytest.mark.parametrize(
        "remote_path",
        [
            "decisions/003-x.md",
            "decisions\\003-x.md",
            "decisions/003-x:stream.md",
            "decisions/003-café.md",
            "decisions/003-x.md.",
            "Decisions/003-X.MD",
            "decisions/a%2Fb.md",
        ],
    )
    def test_a_path_round_trips_through_a_flat_safe_filename(self, remote_path):
        name = backup_name(remote_path, "k" * 16)

        assert "/" not in name and "\\" not in name and ":" not in name
        assert not name.endswith(".")
        digest, decoded = _parse_backup_name(name)
        assert decoded == remote_path
        assert digest == path_key(remote_path)

    def test_an_encoded_path_is_written_and_read_back(self, store):
        remote_path = "decisions\\003-x.md"

        saved = save_quarantine_backup(store, remote_path, b"body\n", '"etag"')

        assert saved.is_file()
        assert saved.read_bytes() == b"body\n"
        assert [item.remote_path for item in list_quarantine_backups(store)] == [remote_path]

    def test_a_very_long_path_stays_inside_the_filename_limit(self, store):
        remote_path = f"decisions/{'a' * 193}.md"

        saved = save_quarantine_backup(store, remote_path, b"body\n", '"etag"')

        assert len(saved.name.encode("utf-8")) < 255
        listed = list_quarantine_backups(store)
        assert len(listed) == 1
        # The display is capped and says so; identity is the digest.
        assert listed[0].complete_path is False
        assert listed[0].label.endswith("...")
        assert listed[0].path_digest == path_key(remote_path)

    def test_a_capped_path_still_resolves_once_installed(self, store):
        remote_path = f"decisions/{'a' * 193}.md"
        save_quarantine_backup(store, remote_path, b"body\n", '"etag"')
        assert len(unresolved_quarantines(store)) == 1

        state = SyncState()
        state.files[remote_path] = FileState(local_sha256="abc", remote_etag='"etag"')
        save_state(store, state)

        assert unresolved_quarantines(store) == []


class TestHeadingFormsStayBound:
    """The lax reader and the strict rewriter must not drift apart.

    Two modules describe the same heading: the kernel's parser decides what a
    decision file is, and this layer's rewriter decides which of those it may
    renumber. If the strict form ever stopped being a subset of the parsed one,
    a renumber would produce a file the kernel no longer reads - so the
    relationship is asserted rather than assumed.
    """

    _HEADINGS = [
        "# 003 — Em dash",
        "# 003 - Hyphen",
        "#  003 — Extra space",
        "# 3 — Unpadded",
        "# 0003 — Over padded",
        "## 003 — Wrong level",
        "#003 — No space",
        "# 003 : Wrong separator",
    ]

    @pytest.mark.parametrize("heading", _HEADINGS)
    def test_strict_is_a_subset_of_lax(self, heading):
        text = f"{heading}\nbody\n"

        if has_rewritable_heading(text, 3):
            assert heading_number(text) == 3

    @pytest.mark.parametrize("heading", _HEADINGS)
    def test_a_rewrite_never_makes_a_readable_decision_unreadable(self, heading):
        """The invariant a renumber depends on: if the kernel could read the
        file before, it can read it after, and its heading agrees with the
        filename the rename gives it."""
        body = (
            "---\ndate: 2026-08-11\nversion: 1\nstatus: active\nconfidence: high\n"
            "decision_type: null\nreversibility: null\nsource: null\nfiles_affected: []\n"
            "supersedes: null\nsuperseded_by: null\n---\n\n"
            f"{heading}\n\n## Decision\n\nBody.\n"
        )
        try:
            parse_decision(body, "003-x.md")
        except ValueError:
            return
        if not has_rewritable_heading(body, 3):
            return

        rewritten = rewrite_decision_heading(body, 3, 44)

        assert heading_number(rewritten) == 44
        parsed = parse_decision(rewritten, "044-renamed.md")
        assert parsed.num == 44
        assert heading_number(parsed.content) == 44

    def test_the_kernel_reads_every_heading_the_rewriter_accepts(self):
        """The em-dash form is the one nauro-core's parser requires; the
        rewriter also accepts a hyphen, which the parser does not. That gap is
        deliberate and one-directional: a hyphen heading is renumberable but
        was never a parseable decision to begin with, so no renumber can turn a
        readable file into an unreadable one."""
        em_dash = "# 003 — Title\n\n## Decision\n\nBody.\n"
        frontmatter = (
            "---\ndate: 2026-08-11\nversion: 1\nstatus: active\nconfidence: high\n"
            "decision_type: null\nreversibility: null\nsource: null\nfiles_affected: []\n"
            "supersedes: null\nsuperseded_by: null\n---\n\n"
        )

        assert has_rewritable_heading(em_dash, 3) is True
        assert parse_decision(frontmatter + em_dash, "003-x.md").num == 3

        hyphen = "# 003 - Title\n\n## Decision\n\nBody.\n"
        assert has_rewritable_heading(hyphen, 3) is True
        with pytest.raises(ValueError):
            parse_decision(frontmatter + hyphen, "003-x.md")
