"""Tests for non-destructive decision versioning."""

from pathlib import Path

import pytest

from nauro.store.reader import _list_decisions
from nauro.templates.scaffolds import scaffold_project_store
from tests._writer_compat import append_decision


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
