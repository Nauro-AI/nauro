"""Tests for nauro init --demo."""

import json
from pathlib import Path

import pytest

from nauro import constants
from nauro.demo import create_demo_project
from nauro.store.reader import _list_decisions
from nauro.store.snapshot import list_snapshots
from tests.conftest import read_project_context


@pytest.fixture()
def demo_store(tmp_path: Path) -> Path:
    """Create a demo project in a temp directory."""
    store_path = tmp_path / "projects" / "demo-project"
    create_demo_project(store_path)
    return store_path


class TestDemoProjectStructure:
    def test_all_store_files_exist(self, demo_store):
        """Demo project should have the same files as a real project."""
        assert (demo_store / constants.PROJECT_MD).exists()
        assert (demo_store / constants.STATE_CURRENT_FILENAME).exists()
        assert (demo_store / constants.STACK_MD).exists()
        assert (demo_store / constants.OPEN_QUESTIONS_MD).exists()
        assert (demo_store / constants.DECISIONS_DIR).is_dir()
        assert (demo_store / constants.SNAPSHOTS_DIR).is_dir()

    def test_has_thirteen_decisions(self, demo_store):
        decisions = _list_decisions(demo_store)
        assert len(decisions) == 13

    def test_decisions_have_correct_format(self, demo_store):
        """Decisions should match the format produced by writer.py."""
        decisions = _list_decisions(demo_store)
        for d in decisions:
            assert d.title, f"Decision {d.num} has no title"
            assert d.rationale, f"Decision {d.num} has no rationale"
            assert d.status.value in ("active", "superseded")
            assert d.confidence.value in ("high", "medium", "low")

    def test_decision_titles(self, demo_store):
        decisions = _list_decisions(demo_store)
        titles = [d.title for d in decisions]
        assert "On-device storage, no cloud account" in titles
        assert "One-time purchase, no subscription" in titles
        assert "Amounts stored in integer cents, never floating point" in titles
        assert "Native mobile app over web app" in titles
        assert "Passcode and biometric lock, no user accounts" in titles
        assert "Envelope budgeting method" in titles
        assert "No ads, no data monetization" in titles
        assert (
            "Unified transaction pipeline for categorization, formatting, and de-duplication"
            in titles
        )
        assert "Pay-cycle budget periods" in titles

    def test_has_snapshot(self, demo_store):
        snapshots = list_snapshots(demo_store)
        assert len(snapshots) >= 1

    def test_snapshot_contains_all_files(self, demo_store):
        snapshots = list_snapshots(demo_store)
        ver = snapshots[0]["version"]
        snapshot_path = demo_store / constants.SNAPSHOTS_DIR / f"v{ver:03d}.json"
        data = json.loads(snapshot_path.read_text())
        assert constants.PROJECT_MD in data["files"]
        assert constants.STATE_CURRENT_FILENAME in data["files"]
        assert any(k.startswith(constants.DECISIONS_DIR + "/") for k in data["files"])

    def test_state_has_content(self, demo_store):
        content = (demo_store / constants.STATE_CURRENT_FILENAME).read_text()
        assert "# Current State" in content

    def test_open_questions_has_content(self, demo_store):
        content = (demo_store / constants.OPEN_QUESTIONS_MD).read_text()
        assert "roll over" in content
        assert "export" in content

    def test_project_md_has_content(self, demo_store):
        content = (demo_store / constants.PROJECT_MD).read_text()
        assert "Pennykeep" in content
        assert "Goals" in content


class TestDemoSupersession:
    """The demo store carries the two supersession structures the graph shows:
    a three-into-one consolidation fan and a two-step chain. These assert the
    on-disk convention propose_decision's supersede path writes (scalar
    ``supersedes`` on the retirer, ``superseded_by`` + superseded status on each
    retired decision) so the demo cannot drift from real writer output.
    """

    def test_active_and_superseded_split(self, demo_store):
        decisions = _list_decisions(demo_store)
        by_num = {d.num: d for d in decisions}
        superseded = sorted(d.num for d in decisions if d.status.value == "superseded")
        assert superseded == [8, 9, 10, 11]
        for num in (1, 2, 3, 4, 5, 6, 7, 12, 13):
            assert by_num[num].status.value == "active"

    def test_consolidation_fan_edges_are_symmetric(self, demo_store):
        """Three retired decisions each point back at the one retirer, and the
        retirer carries a scalar supersedes at one of them (the earliest)."""
        by_num = {d.num: d for d in _list_decisions(demo_store)}
        retirer = by_num[13]
        assert retirer.status.value == "active"
        assert retirer.supersedes == "8"
        assert retirer.superseded_by is None
        for retired_num in (8, 9, 10):
            retired = by_num[retired_num]
            assert retired.status.value == "superseded"
            assert retired.superseded_by == "13"
            assert retired.supersedes is None

    def test_short_chain_edges_are_symmetric(self, demo_store):
        by_num = {d.num: d for d in _list_decisions(demo_store)}
        older, newer = by_num[11], by_num[12]
        assert older.status.value == "superseded"
        assert older.superseded_by == "12"
        assert older.supersedes is None
        assert newer.status.value == "active"
        assert newer.supersedes == "11"
        assert newer.superseded_by is None

    def test_superseded_files_parse_with_refs_on_disk(self, demo_store):
        """The superseded entries reach the parser from disk with their refs
        intact. The status=superseded validator requires a superseded_by ref,
        so a missing one would surface here as a parse failure rather than a
        silent count mismatch."""
        files = sorted((demo_store / constants.DECISIONS_DIR).glob("*.md"))
        bodies = {f.name: f.read_text(encoding="utf-8") for f in files}
        # Each retired decision file carries its superseded status and back-ref.
        for stem in ("008", "009", "010", "011"):
            match = next(name for name in bodies if name.startswith(stem))
            assert "status: superseded" in bodies[match]
            assert "superseded_by:" in bodies[match]


class TestDemoWithContext:
    def test_l0_context_works(self, demo_store):
        """L0 context should include decision summaries."""
        context = read_project_context(demo_store, level=0)
        assert "On-device" in context or "cloud" in context

    def test_l1_context_works(self, demo_store):
        """L1 context should include full decisions."""
        context = read_project_context(demo_store, level=1)
        assert "Rejected Alternatives" in context

    def test_l2_context_works(self, demo_store):
        """L2 context should include everything."""
        context = read_project_context(demo_store, level=2)
        assert "Pennykeep" in context
        assert "SQLite" in context
