"""Tests for nauro_core.pending — PendingStore lifecycle."""

from datetime import UTC, datetime, timedelta

from nauro_core.pending import PendingStore


class TestPendingStore:
    def test_store_and_retrieve(self):
        store = PendingStore()
        cid = store.store({"title": "Test"}, {"tier": 1})
        result = store.get(cid)
        assert result is not None
        assert result["proposal"]["title"] == "Test"
        assert result["validation_result"]["tier"] == 1
        assert "created_at" in result

    def test_confirm_id_is_unique(self):
        store = PendingStore()
        cid1 = store.store({"title": "A"}, {})
        cid2 = store.store({"title": "B"}, {})
        assert cid1 != cid2

    def test_missing_confirm_id_returns_none(self):
        store = PendingStore()
        assert store.get("nonexistent-id") is None

    def test_remove(self):
        store = PendingStore()
        cid = store.store({"title": "Test"}, {})
        store.remove(cid)
        assert store.get(cid) is None

    def test_remove_nonexistent_is_noop(self):
        store = PendingStore()
        store.remove("nonexistent-id")  # should not raise

    def test_expire_removes_old_entries(self):
        store = PendingStore()
        cid = store.store({"title": "Old"}, {})
        # Backdate the entry
        store._pending[cid]["created_at"] = datetime.now(UTC) - timedelta(minutes=15)
        store.expire()
        assert store.get(cid) is None

    def test_expire_keeps_fresh_entries(self):
        store = PendingStore()
        cid = store.store({"title": "Fresh"}, {})
        store.expire()
        assert store.get(cid) is not None

    def test_auto_expire_on_store(self):
        store = PendingStore()
        old_cid = store.store({"title": "Old"}, {})
        store._pending[old_cid]["created_at"] = datetime.now(UTC) - timedelta(minutes=15)
        # Storing a new entry triggers expire
        store.store({"title": "New"}, {})
        assert store.get(old_cid) is None

    def test_auto_expire_on_get(self):
        store = PendingStore()
        old_cid = store.store({"title": "Old"}, {})
        store._pending[old_cid]["created_at"] = datetime.now(UTC) - timedelta(minutes=15)
        # Getting triggers expire
        assert store.get(old_cid) is None

    def test_clear_all(self):
        store = PendingStore()
        store.store({"title": "A"}, {})
        store.store({"title": "B"}, {})
        assert len(store) == 2
        store.clear_all()
        assert len(store) == 0

    def test_len(self):
        store = PendingStore()
        assert len(store) == 0
        store.store({"title": "A"}, {})
        assert len(store) == 1
        store.store({"title": "B"}, {})
        assert len(store) == 2

    def test_multiple_stores_independent(self):
        store1 = PendingStore()
        store2 = PendingStore()
        cid1 = store1.store({"title": "A"}, {})
        assert store2.get(cid1) is None

    def test_confirm_id_format_uuid(self):
        store = PendingStore()
        cid = store.store({"title": "Test"}, {})
        # UUID4 format: 8-4-4-4-12 hex chars
        parts = cid.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
