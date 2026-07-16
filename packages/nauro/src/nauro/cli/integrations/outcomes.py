"""Typed outcomes the setup codecs return for the render layer to emit."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RawLine:
    """A pre-rendered advisory or status string carried verbatim."""

    text: str


# Single-member for now; later increments widen this into a union as each
# codec gains its own structured outcome type.
ArtifactOutcome = RawLine
