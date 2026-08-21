"""Fail-open runner for the ancillary steps that trail a committed write.

Once a judgment write has committed, the artifacts derived from it are ancillary:
the snapshot capture, the ``AGENTS.md`` regeneration and the cloud push. A
failure in one is reported, never converted into a failure of the write that
already succeeded. The CLI names the degraded step and exits 0, the MCP adapters
keep their success envelope, every remaining step still runs when an earlier one
degrades, and each artifact self-heals on the next write or sync.

Every step is guarded here, the injected push included: this module owns the
posture for whatever a caller injects rather than trusting each callee.

Boundary: ``nauro sync`` and the setup wiring paths are not routed through this
seam. There the capture or the regeneration is the command's primary work, so a
failure stays a failure and exits nonzero.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from nauro.store.snapshot import capture_snapshot
from nauro.templates.agents_md_regen import warn_then_regen

if TYPE_CHECKING:
    from nauro.sync.push import PushReport

logger = logging.getLogger("nauro.store.post_commit")


class AncillaryStep(str, Enum):
    """The derived-artifact steps that run after a write has committed."""

    SNAPSHOT = "snapshot capture"
    AGENTS_MD = "AGENTS.md regeneration"
    PUSH = "cloud push"


class DegradedStep(BaseModel):
    """One ancillary step that did not complete, and why."""

    model_config = ConfigDict(frozen=True)

    step: AncillaryStep
    reason: str

    @property
    def warning(self) -> str:
        """The human-facing line naming the degraded step."""
        return (
            f"  Warning: the write was recorded, but {self.step.value} failed: {self.reason}\n"
            "  Run 'nauro sync' to rebuild the derived files."
        )


class PostCommitOutcome(BaseModel):
    """What the ancillary steps did for one committed write."""

    model_config = ConfigDict(frozen=True)

    degraded: tuple[DegradedStep, ...] = ()
    updated_repos: tuple[Path, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        """One line per degraded step, in the order the steps ran."""
        return tuple(item.warning for item in self.degraded)


def run_post_commit(
    store_path: Path,
    *,
    snapshot_trigger: str | None = None,
    regenerate_agents_md: bool = False,
    warn: Callable[[str], None] | None = None,
    surface_bridge_notices: bool = True,
    push: Callable[[Path], PushReport | None] | None = None,
) -> PostCommitOutcome:
    """Return the outcome naming every degraded step and each regenerated ``AGENTS.md``.
    A ``None`` ``snapshot_trigger`` skips the capture and ``push`` runs last whatever
    degraded; ``warn`` never carries a degraded step, only its own per-repo notices.
    """
    degraded: list[DegradedStep] = []
    updated_repos: list[Path] = []

    if snapshot_trigger is not None:
        try:
            capture_snapshot(store_path, trigger=snapshot_trigger)
        except Exception as exc:
            logger.debug("snapshot capture failed for %s", store_path.name, exc_info=True)
            degraded.append(
                DegradedStep(step=AncillaryStep.SNAPSHOT, reason=f"{type(exc).__name__}: {exc}")
            )

    if regenerate_agents_md:
        try:
            updated_repos = warn_then_regen(
                store_path.name,
                store_path,
                warn=warn,
                surface_bridge_notices=surface_bridge_notices,
            )
        except Exception as exc:
            logger.debug("AGENTS.md regeneration failed for %s", store_path.name, exc_info=True)
            degraded.append(
                DegradedStep(step=AncillaryStep.AGENTS_MD, reason=f"{type(exc).__name__}: {exc}")
            )

    if push is not None:
        try:
            push_report = push(store_path)
        except Exception as exc:
            logger.debug("cloud push failed for %s", store_path.name, exc_info=True)
            degraded.append(
                DegradedStep(step=AncillaryStep.PUSH, reason=f"{type(exc).__name__}: {exc}")
            )
        else:
            if push_report is not None and not push_report.is_complete:
                degraded.append(DegradedStep(step=AncillaryStep.PUSH, reason=push_report.warning))

    return PostCommitOutcome(degraded=tuple(degraded), updated_repos=tuple(updated_repos))


# The one free-text field every MCP write envelope shares. Degraded steps ride
# here rather than in a field of their own: the envelope shapes are pinned, and
# a new key would be a contract change for a condition that is not a failure of
# the write.
ASSESSMENT_FIELD = "assessment"


def surface_post_commit(
    envelope: dict,
    outcome: PostCommitOutcome,
    *,
    extra_warnings: Sequence[str] = (),
) -> dict:
    """Route a post-commit outcome's warnings into one write tool's envelope.

    Mutates the envelope in place and returns it, adding the key only when warned.
    """
    notes = [*extra_warnings, *outcome.warnings]
    if not notes:
        return envelope
    existing = envelope.get(ASSESSMENT_FIELD)
    envelope[ASSESSMENT_FIELD] = "\n\n".join([existing, *notes] if existing else notes)
    return envelope


def with_assessment(message: str, envelope: dict) -> str:
    """Return ``message`` carrying the envelope's assessment text, if it has any.

    A message with nothing to add comes back unchanged.
    """
    assessment = envelope.get(ASSESSMENT_FIELD)
    return f"{message}\n\n{assessment}" if assessment else message
