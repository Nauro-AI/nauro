"""Test helper that runs one generation refresh through both phases."""

from __future__ import annotations

from nauro.sync import generation_refresh as refresh


def recover_generation_refresh(binding, *, actor, session=None):
    """Prepare and commit one refresh through module attributes, so patches apply."""
    prepared = refresh.prepare_generation_refresh(binding, actor=actor, session=session)
    return refresh.commit_generation_refresh(prepared, session=session)
