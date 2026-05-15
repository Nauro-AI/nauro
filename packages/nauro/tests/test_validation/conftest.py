"""Shared fixtures for the validation test subdirectory."""

import pytest

from nauro.validation.pending import clear_all


@pytest.fixture(autouse=True)
def _clear_pending():
    """Reset the in-process pending-decision registry around every test.

    ``nauro.validation.pending`` is module-global state used by the propose →
    confirm flow. Without this, a leftover pending entry from one test bleeds
    into the next and produces non-deterministic failures.
    """
    clear_all()
    yield
    clear_all()
