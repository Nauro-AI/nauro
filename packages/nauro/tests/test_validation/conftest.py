"""Shared fixtures for the validation test subdirectory."""

import pytest
from nauro_core.operations.propose_decision import _get_pending_store


@pytest.fixture(autouse=True)
def _clear_pending():
    """Reset the in-process pending-decision registry around every test.

    The kernel's pending store is module-global state used by the propose →
    confirm flow. Without this, a leftover pending entry from one test bleeds
    into the next and produces non-deterministic failures.
    """
    _get_pending_store().clear_all()
    yield
    _get_pending_store().clear_all()
