"""Tests for nauro.sync.merge."""

from nauro_core.decision_model import parse_decision

from nauro.sync.merge import (
    _set_union_markdown,
    detect_conflict,
    resolve_conflict,
    should_skip,
)
from nauro.sync.state import FileState, SyncState


class TestShouldSkip:
    def test_sync_state_file(self):
        assert should_skip(".sync-state.json") is True

    def test_normal_file(self):
        assert should_skip("project.md") is False

    def test_decision_file(self):
        assert should_skip("decisions/001-foo.md") is False

    def test_default_graph_output_is_skipped(self):
        # The graph command's default output carries a generation timestamp that
        # changes every run, so its sha never settles; syncing it would re-push
        # the artifact endlessly and fan it out to every collaborator.
        assert should_skip("nauro-graph.html") is True

    def test_graph_output_under_subdir_is_not_skipped(self):
        # Only the default store-root filename is guarded; a path the user chose
        # via --output is their explicit choice and may sync.
        assert should_skip("reports/nauro-graph.html") is False

    def test_lock_artifacts_are_skipped(self):
        # filelock keeps Unix lock files after release as of 3.29.5, so store
        # writes leave these behind; they are concurrency plumbing, not content.
        assert should_skip("decisions/.lock") is True
        assert should_skip("decisions/002-use-redis.md.lock") is True
        assert should_skip("open-questions.md.rmwlock") is True
        assert should_skip("snapshots/.lock") is True
        # write_file's one non-markdown target, the decision-hash index.
        assert should_skip(".decision-hashes.json.lock") is True

    def test_non_artifact_lock_names_still_sync(self):
        # Only the store's own artifact shapes are skipped; user content that
        # happens to end in .lock is not.
        assert should_skip("context/poetry.lock") is False
        assert should_skip("uv.lock") is False

    def test_journal_is_never_synced(self):
        # The write-path provenance journal is store-local in v1: its events
        # log and its lock are both excluded from cloud sync.
        assert should_skip("journal/events.jsonl") is True
        assert should_skip("journal/.lock") is True


class TestDetectConflict:
    def test_no_previous_state(self):
        state = SyncState()
        assert detect_conflict("new.md", state, "sha1", '"etag1"') is False

    def test_only_local_changed(self):
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256="old_sha", remote_etag='"same_etag"')
        assert detect_conflict("test.md", state, "new_sha", '"same_etag"') is False

    def test_only_remote_changed(self):
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256="same_sha", remote_etag='"old_etag"')
        assert detect_conflict("test.md", state, "same_sha", '"new_etag"') is False

    def test_both_changed(self):
        state = SyncState()
        state.files["test.md"] = FileState(local_sha256="old_sha", remote_etag='"old_etag"')
        assert detect_conflict("test.md", state, "new_sha", '"new_etag"') is True


class TestResolveConflict:
    def test_lww_for_state_md(self, tmp_path):
        """state.md uses last-write-wins with backup."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "state.md"
        local_file.write_text("local state content")
        remote_content = b"remote state content"

        result = resolve_conflict(project_path, local_file, remote_content, "state.md")

        assert result == b"local state content"
        backup_dir = project_path / ".conflict-backup"
        assert backup_dir.exists()
        backups = list(backup_dir.iterdir())
        assert len(backups) == 1
        assert backups[0].read_bytes() == remote_content

    def test_lww_for_project_md(self, tmp_path):
        """project.md uses last-write-wins with backup."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "project.md"
        local_file.write_text("local project content")
        remote_content = b"remote project content"

        result = resolve_conflict(project_path, local_file, remote_content, "project.md")

        assert result == b"local project content"
        backup_dir = project_path / ".conflict-backup"
        assert backup_dir.exists()

    def test_lww_for_snapshots(self, tmp_path):
        """Snapshot files use last-write-wins."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "snapshots").mkdir()
        local_file = project_path / "snapshots" / "v001.json"
        local_file.write_text('{"local": true}')
        remote_content = b'{"remote": true}'

        result = resolve_conflict(project_path, local_file, remote_content, "snapshots/v001.json")

        assert result == b'{"local": true}'

    def test_lww_for_decisions(self, tmp_path):
        """Decision files use last-write-wins with backup of the losing version.

        A decision file is immutable per number; interleaving two divergent
        versions line-by-line would corrupt the file. So a content conflict
        keeps the local copy whole and backs up the remote loser instead.
        """
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "decisions").mkdir()
        local_file = project_path / "decisions" / "001-foo.md"
        local_file.write_text("# Decision 001\nLocal addition\n")
        remote_content = b"# Decision 001\nRemote addition\n"

        result = resolve_conflict(project_path, local_file, remote_content, "decisions/001-foo.md")

        # Local copy is kept whole, never interleaved with remote.
        assert result == b"# Decision 001\nLocal addition\n"
        backup_dir = project_path / ".conflict-backup"
        assert backup_dir.exists()
        backups = list(backup_dir.iterdir())
        assert len(backups) == 1
        assert backups[0].read_bytes() == remote_content

    def test_update_vs_supersede_conflict_survivor_parses(self, tmp_path):
        """An update-vs-supersede conflict on the same number stays parseable.

        Both sides are in-place rewrites of decision 042: local marks it
        superseded, remote bumps its version with new rationale. A union merge
        would interleave the two frontmatter blocks and bodies into a single
        file that no longer parses. Last-write-wins keeps the local rewrite
        whole and backs the remote loser up, so the survivor is one decision
        that ``parse_decision`` accepts.
        """
        local_text = (
            "---\n"
            "date: 2026-06-01\n"
            "confidence: high\n"
            "version: 2\n"
            "status: superseded\n"
            'superseded_by: "99"\n'
            "---\n"
            "\n"
            "# 042 — Cache layer choice\n"
            "\n"
            "## Decision\n"
            "\n"
            "Superseded by the persisted-index approach.\n"
        )
        remote_text = (
            "---\n"
            "date: 2026-06-01\n"
            "confidence: medium\n"
            "version: 2\n"
            "status: active\n"
            "---\n"
            "\n"
            "# 042 — Cache layer choice\n"
            "\n"
            "## Decision\n"
            "\n"
            "Revised in place to use a write-through cache.\n"
        )

        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "decisions").mkdir()
        local_file = project_path / "decisions" / "042-cache-layer.md"
        local_file.write_text(local_text)
        remote_content = remote_text.encode("utf-8")

        result = resolve_conflict(
            project_path, local_file, remote_content, "decisions/042-cache-layer.md"
        )

        # The survivor is the local rewrite, whole and parseable as ONE decision.
        assert result == local_text.encode("utf-8")
        survivor = parse_decision(result.decode("utf-8"), "042-cache-layer.md")
        assert survivor.num == 42
        assert survivor.status.value == "superseded"
        assert survivor.superseded_by == "99"

        # The remote loser was backed up rather than interleaved into the file.
        backups = list((project_path / ".conflict-backup").iterdir())
        assert len(backups) == 1
        assert backups[0].read_bytes() == remote_content

    def test_set_union_for_open_questions(self, tmp_path):
        """open-questions.md merges by set-union: lines unique to each side survive."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "open-questions.md"
        local_file.write_text("- Question 1\n- Local question\n")
        remote_content = b"- Question 1\n- Remote question\n"

        result = resolve_conflict(project_path, local_file, remote_content, "open-questions.md")

        result_str = result.decode()
        assert result_str.count("Question 1") == 1
        assert "- Local question" in result_str
        assert "- Remote question" in result_str


class TestSetUnionMarkdown:
    """Section-aware set-union merge for open-questions.md and state_history.md."""

    def test_duplicate_top_level_header_collapses(self):
        """Both sides carry the same preamble; merge keeps a single header."""
        local = b"# Open Questions\n\n- A\n\n## Resolved\n- R1\n\n## Active\n- ACT1\n"
        remote = b"# Open Questions\n\n- A\n\n## Resolved\n- R1\n\n## Active\n- ACT1\n"

        result = _set_union_markdown(local, remote).decode()

        assert result.count("# Open Questions") == 1
        assert result.count("## Resolved") == 1
        assert result.count("## Active") == 1
        assert result.count("- A\n") == 1
        assert result.count("- R1\n") == 1
        assert result.count("- ACT1\n") == 1

    def test_unique_entries_local_first_order(self):
        """Local entries appear first; remote-only entries appended."""
        local = b"# Open Questions\n- A\n- B\n- C\n"
        remote = b"# Open Questions\n- A\n- B\n- D\n"

        result = _set_union_markdown(local, remote).decode()

        assert result.count("- A") == 1
        assert result.count("- B") == 1
        assert result.count("- C") == 1
        assert result.count("- D") == 1
        # Local-first ordering: A, B, C, then remote-only D.
        pos_a = result.find("- A")
        pos_b = result.find("- B")
        pos_c = result.find("- C")
        pos_d = result.find("- D")
        assert pos_a < pos_b < pos_c < pos_d

    def test_state_history_no_sections(self):
        """state_history.md is preamble-only; merge does line-set union."""
        local = b"L1\nL2\nL3\n"
        remote = b"L1\nL3\nR1\n"

        result = _set_union_markdown(local, remote).decode()

        assert result.count("L1") == 1
        assert result.count("L2") == 1
        assert result.count("L3") == 1
        assert result.count("R1") == 1
        # Local-first order preserved.
        assert result.find("L1") < result.find("L2") < result.find("L3") < result.find("R1")

    def test_remote_preamble_entry_lands_in_preamble(self):
        """A remote preamble entry merges into the preamble, not into a section."""
        local = b"# Open Questions\n- local-q\n\n## Resolved\n- old-resolved\n"
        remote = b"# Open Questions\n- remote-q\n\n## Resolved\n- new-resolved\n"

        result = _set_union_markdown(local, remote).decode()

        # Split on the Resolved header to isolate the preamble from the section.
        preamble, _, resolved_section = result.partition("## Resolved")
        assert "- local-q" in preamble
        assert "- remote-q" in preamble
        assert "- local-q" not in resolved_section
        assert "- remote-q" not in resolved_section
        assert "- old-resolved" in resolved_section
        assert "- new-resolved" in resolved_section

    def test_blank_lines_preserved(self):
        """Blank lines are passed through, not collapsed into a single blank."""
        local = b"# H\n\n- A\n\n## Resolved\n\n- R1\n"
        remote = b"# H\n\n- B\n\n## Resolved\n\n- R2\n"

        result = _set_union_markdown(local, remote).decode()

        # There should still be a blank line after the top-level header in the
        # merged output (we didn't collapse it away).
        assert "# H\n\n" in result
        # And a blank line right after the ## Resolved header.
        assert "## Resolved\n\n" in result

    def test_idempotent_on_identical_inputs(self):
        """merge(content, content) yields content with no duplicate non-blank lines."""
        content = b"# Open Questions\n\n- A\n- B\n\n## Resolved\n- R1\n\n## Active\n- ACT1\n"

        once = _set_union_markdown(content, content).decode()
        twice = _set_union_markdown(once.encode(), once.encode()).decode()

        # No non-blank line duplicated on the first or second pass. Match whole
        # lines (split on "\n") so a prefix like "- A" does not match "- ACT1".
        once_lines = once.split("\n")
        twice_lines = twice.split("\n")
        for token in (
            "# Open Questions",
            "## Resolved",
            "## Active",
            "- A",
            "- B",
            "- R1",
            "- ACT1",
        ):
            assert once_lines.count(token) == 1, f"{token!r} repeats in {once!r}"
            assert twice_lines.count(token) == 1, f"{token!r} repeats in {twice!r}"

    def test_relocated_vs_unrelocated_skew_degrades_to_annotated_stray(self):
        """Self-heal merge-skew guard.

        When one side relocated a resolved entry below ``## Resolved`` and the
        other still carries it as an annotated stray above the divider, the
        set-union merge yields no duplicate id and preserves the resolution
        annotation. The entry may legally reappear above the divider as an
        annotated stray — the status quo the self-heal decision verified
        against, not corruption or data loss.
        """
        annotation = "- [Resolved by D42 on 2026-05-14] [Q5] resolved body"
        normalized = ("# Open Questions\n\n## Resolved\n\n" + annotation + "\n").encode()
        unrelocated = ("# Open Questions\n\n" + annotation + "\n\n## Resolved\n").encode()

        result = _set_union_markdown(normalized, unrelocated).decode()

        # No duplicate id: the annotation line survives exactly once.
        assert result.split("\n").count(annotation) == 1
        # The resolution annotation is preserved.
        assert "[Resolved by D42 on 2026-05-14] [Q5]" in result
        # Exactly one divider.
        assert result.count("## Resolved") == 1

    def test_decisions_do_not_route_to_set_union(self, tmp_path, monkeypatch):
        """Decision conflicts resolve by last-write-wins, not _set_union_markdown."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "decisions").mkdir()
        local_file = project_path / "decisions" / "001-foo.md"
        local_file.write_text("# Decision 001\nLocal body\n")
        remote_content = b"# Decision 001\nRemote body\n"

        calls: list[str] = []

        def spy(local: bytes, remote: bytes) -> bytes:
            calls.append("set_union")
            return local

        monkeypatch.setattr("nauro.sync.merge._set_union_markdown", spy)

        resolve_conflict(project_path, local_file, remote_content, "decisions/001-foo.md")

        assert calls == []
        # Last-write-wins backs the remote loser up rather than interleaving it.
        backups = list((project_path / ".conflict-backup").iterdir())
        assert len(backups) == 1
        assert backups[0].read_bytes() == remote_content

    def test_open_questions_routes_to_set_union(self, tmp_path, monkeypatch):
        """open-questions.md conflicts go through _set_union_markdown."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "open-questions.md"
        local_file.write_text("# Open Questions\n- local\n")
        remote_content = b"# Open Questions\n- remote\n"

        calls: list[tuple[bytes, bytes]] = []

        def spy(local: bytes, remote: bytes) -> bytes:
            calls.append((local, remote))
            return b"merged"

        monkeypatch.setattr("nauro.sync.merge._set_union_markdown", spy)

        result = resolve_conflict(project_path, local_file, remote_content, "open-questions.md")

        assert result == b"merged"
        assert len(calls) == 1
        assert calls[0][0] == b"# Open Questions\n- local\n"
        assert calls[0][1] == remote_content

    def test_state_history_routes_to_set_union(self, tmp_path, monkeypatch):
        """state_history.md conflicts go through _set_union_markdown."""
        project_path = tmp_path / "project"
        project_path.mkdir()
        local_file = project_path / "state_history.md"
        local_file.write_text("local line\n")
        remote_content = b"remote line\n"

        calls: list[tuple[bytes, bytes]] = []

        def spy(local: bytes, remote: bytes) -> bytes:
            calls.append((local, remote))
            return b"merged"

        monkeypatch.setattr("nauro.sync.merge._set_union_markdown", spy)

        result = resolve_conflict(project_path, local_file, remote_content, "state_history.md")

        assert result == b"merged"
        assert len(calls) == 1

    def test_real_world_duplicate_header_regression(self):
        """A file already corrupted with a duplicated top-level header heals on resync.

        Mirrors the observed open-questions.md pathology: the entire document
        appears twice end-to-end. After merging two copies, the result should
        have a single top-level header and a single copy of each section.
        """
        corrupted = (
            b"# Open Questions\n"
            b"\n"
            b"- old-entry\n"
            b"\n"
            b"## Resolved\n"
            b"- old-resolved\n"
            b"\n"
            b"## Active\n"
            b"- old-active\n"
            b"# Open Questions\n"
            b"\n"
            b"- old-entry\n"
            b"\n"
            b"## Resolved\n"
            b"- old-resolved\n"
            b"\n"
            b"## Active\n"
            b"- old-active\n"
        )

        result = _set_union_markdown(corrupted, corrupted).decode()

        assert result.count("# Open Questions") == 1
        assert result.count("## Resolved") == 1
        assert result.count("## Active") == 1
        assert result.count("- old-entry") == 1
        assert result.count("- old-resolved") == 1
        assert result.count("- old-active") == 1
