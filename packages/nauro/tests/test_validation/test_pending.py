"""Tests for pending confirmation store."""

from datetime import UTC, datetime, timedelta

import pytest

from nauro.validation.pending import (
    _store,
    clear_all,
    expire_pending,
    get_pending,
    remove_pending,
    store_pending,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_all()
    yield
    clear_all()


def test_store_and_retrieve():
    confirm_id = store_pending(
        {"title": "Test"},
        {"status": "pending_confirmation"},
    )
    assert confirm_id is not None

    pending = get_pending(confirm_id)
    assert pending is not None
    assert pending["proposal"]["title"] == "Test"


def test_get_nonexistent():
    assert get_pending("nonexistent-uuid") is None


def test_remove():
    confirm_id = store_pending({"title": "Test"}, {})
    remove_pending(confirm_id)
    assert get_pending(confirm_id) is None


def test_expire_old_entries():
    confirm_id = store_pending({"title": "Old"}, {})
    # Manually backdate via internal store
    _store._pending[confirm_id]["created_at"] = datetime.now(UTC) - timedelta(minutes=15)

    expire_pending()
    assert get_pending(confirm_id) is None


def test_fresh_entries_not_expired():
    confirm_id = store_pending({"title": "Fresh"}, {})
    expire_pending()
    assert get_pending(confirm_id) is not None


def test_clear_all():
    store_pending({"title": "A"}, {})
    store_pending({"title": "B"}, {})
    assert len(_store) == 2

    clear_all()
    assert len(_store) == 0
