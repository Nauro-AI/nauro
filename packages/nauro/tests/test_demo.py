"""Tests for nauro init --demo."""

import json
from pathlib import Path

import pytest

from nauro import constants
from nauro.demo import create_demo_project
from nauro.store.reader import _list_decisions, read_project_context
from nauro.store.snapshot import list_snapshots


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

    def test_has_seven_decisions(self, demo_store):
        decisions = _list_decisions(demo_store)
        assert len(decisions) == 7

    def test_decisions_have_correct_format(self, demo_store):
        """Decisions should match the format produced by writer.py."""
        decisions = _list_decisions(demo_store)
        for d in decisions:
            assert d["title"], f"Decision {d['num']} has no title"
            assert d["rationale"], f"Decision {d['num']} has no rationale"
            assert d["status"] == "active"
            assert d["confidence"] in ("high", "medium", "low")

    def test_decision_titles(self, demo_store):
        decisions = _list_decisions(demo_store)
        titles = [d["title"] for d in decisions]
        assert "Chose PostgreSQL over MongoDB for ACID compliance" in titles
        assert "REST API over GraphQL for simplicity" in titles
        assert "Monorepo with Turborepo over polyrepo" in titles
        assert "SSE over WebSocket for live task updates" in titles
        assert "All processing in request path, no background workers" in titles
        assert "Cursor-based pagination, not offset" in titles
        assert "Hard delete with audit log, no soft deletes" in titles

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
        assert "rate limiting" in content
        assert "Redis" in content

    def test_project_md_has_content(self, demo_store):
        content = (demo_store / constants.PROJECT_MD).read_text()
        assert "TaskFlow" in content
        assert "Goals" in content


class TestDemoWithContext:
    def test_l0_context_works(self, demo_store):
        """L0 context should include decision summaries."""
        context = read_project_context(demo_store, level=0)
        assert "PostgreSQL" in context or "REST" in context

    def test_l1_context_works(self, demo_store):
        """L1 context should include full decisions."""
        context = read_project_context(demo_store, level=1)
        assert "Rejected Alternatives" in context

    def test_l2_context_works(self, demo_store):
        """L2 context should include everything."""
        context = read_project_context(demo_store, level=2)
        assert "PostgreSQL" in context
        assert "Turborepo" in context
