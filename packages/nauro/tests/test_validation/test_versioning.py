"""Tests for non-destructive decision versioning."""

from pathlib import Path

import pytest

from nauro.store.reader import (
    _list_decisions,
    get_decision_history,
    list_active_decisions,
)
from nauro.store.writer import append_decision, supersede_decision, update_decision
from nauro.templates.scaffolds import scaffold_project_store


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store_path = tmp_path / "projects" / "testproj"
    scaffold_project_store("testproj", store_path)
    return store_path


class TestDecisionVersioning:
    def test_new_decision_has_version_1(self, store):
        append_decision(store, "Use Postgres", rationale="Better JSON support for our use case.")
        decisions = _list_decisions(store)
        d = next(d for d in decisions if d.title == "Use Postgres")
        assert d.version == 1
        assert d.status.value == "active"

    def test_new_decision_has_active_status(self, store):
        path = append_decision(store, "Use Redis", rationale="For caching layer in production.")
        content = path.read_text()
        assert "status: active" in content
        assert "version: 1" in content

    def test_backwards_compat_old_decisions(self, store):
        """The scaffolded initial decision has all the required v2 fields."""
        decisions = _list_decisions(store)
        initial = next(d for d in decisions if d.num == 1)
        assert initial.version == 1
        assert initial.status.value == "active"
        assert initial.superseded_by is None
        assert initial.supersedes is None


class TestSupersedeDecision:
    def test_supersede_marks_old_as_superseded(self, store):
        old_path = append_decision(
            store, "Use MySQL", rationale="Cheap and widely available database option."
        )
        old_id = old_path.stem

        new_id = supersede_decision(
            old_id,
            {
                "title": "Switch to Postgres",
                "rationale": "Better JSON support needed.",
                "confidence": "high",
            },
            store,
        )

        decisions = _list_decisions(store)
        old = next(d for d in decisions if d.title == "Use MySQL")
        new = next(d for d in decisions if d.title == "Switch to Postgres")

        assert old.status.value == "superseded"
        assert old.superseded_by == new_id
        assert new.status.value == "active"
        assert new.supersedes == old_id

    def test_superseded_decision_not_in_active_list(self, store):
        old_path = append_decision(
            store, "Use MySQL", rationale="Cheap and widely available database option."
        )
        supersede_decision(
            old_path.stem,
            {
                "title": "Switch to Postgres",
                "rationale": "Better JSON support needed.",
            },
            store,
        )

        active = list_active_decisions(store)
        titles = [d.title for d in active]
        assert "Use MySQL" not in titles
        assert "Switch to Postgres" in titles


class TestUpdateDecision:
    def test_update_increments_version(self, store):
        path = append_decision(store, "Use Postgres", rationale="Better JSON support for our app.")
        decision_id = path.stem

        update_decision(decision_id, "Also great for full-text search.", store)

        decisions = _list_decisions(store)
        d = next(d for d in decisions if d.title == "Use Postgres")
        assert d.version == 2

    def test_update_appends_text(self, store):
        """v2 appends a dated paragraph to rationale, not a `## Update` section."""
        path = append_decision(store, "Use Postgres", rationale="Better JSON support for our app.")
        decision_id = path.stem

        update_decision(decision_id, "Also supports full-text search.", store)

        content = path.read_text()
        assert "*Update (v2)" in content
        assert "full-text search" in content
        assert "JSON support" in content

    def test_multiple_updates(self, store):
        path = append_decision(store, "Use Postgres", rationale="Better JSON support for our app.")
        decision_id = path.stem

        update_decision(decision_id, "Full-text search support.", store)
        update_decision(decision_id, "PostGIS for geospatial queries.", store)

        decisions = _list_decisions(store)
        d = next(d for d in decisions if d.title == "Use Postgres")
        assert d.version == 3

        content = path.read_text()
        assert "*Update (v2)" in content
        assert "*Update (v3)" in content


class TestDecisionHistory:
    def test_history_single_decision(self, store):
        path = append_decision(store, "Use Postgres", rationale="Better JSON support for our app.")
        history = get_decision_history(store, path.stem)
        assert len(history) == 1
        assert history[0].title == "Use Postgres"

    def test_history_supersede_chain(self, store):
        p1 = append_decision(
            store, "Use SQLite", rationale="Simple embedded database for prototype."
        )
        supersede_decision(
            p1.stem,
            {
                "title": "Switch to Postgres",
                "rationale": "Need proper concurrency support.",
            },
            store,
        )

        history = get_decision_history(store, p1.stem)
        assert len(history) == 2
        assert history[0].title == "Use SQLite"
        assert history[1].title == "Switch to Postgres"

    def test_list_active_excludes_superseded(self, store):
        p1 = append_decision(store, "Use MySQL", rationale="Available and cheap option.")
        supersede_decision(
            p1.stem,
            {"title": "Switch to Postgres", "rationale": "Better JSON support."},
            store,
        )

        active = list_active_decisions(store)
        titles = [d.title for d in active]
        assert "Use MySQL" not in titles
        assert "Switch to Postgres" in titles
