"""Echo rendered setup outcomes for the commands that wire surfaces."""

from __future__ import annotations

import typer

from nauro.setup.outcomes import ArtifactOutcome, is_failure
from nauro.setup.render import render


def echo_outcomes(outcomes: list[ArtifactOutcome]) -> bool:
    """Echo every outcome's status lines; True when any write did not land."""
    for outcome in outcomes:
        for line in render(outcome):
            typer.echo(line)
    return any(is_failure(outcome) for outcome in outcomes)
