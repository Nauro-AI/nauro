"""Render typed setup outcomes back into the status lines commands echo."""

from __future__ import annotations

from nauro.cli.integrations.outcomes import ArtifactOutcome, RawLine


def render(outcome: ArtifactOutcome) -> list[str]:
    """Flatten one outcome into the status lines a command echoes."""
    if isinstance(outcome, RawLine):
        return [outcome.text]
    raise TypeError(f"unrenderable outcome: {outcome!r}")
