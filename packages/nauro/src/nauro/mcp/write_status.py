"""Render one local write envelope down to a single truthful line.

The stdio transport answers a write tool with one advisory line rather than
the dict envelope every other local surface returns. That flattening is the
risk this module exists to remove: each wrapper used to test the one status
it cared about and let every other status fall through to its own success
string, so a kernel rejection, a noop, and a store-missing failure all told
the agent the write had landed.

The rule here is one line per status, and the wrapper's success renderer runs
on ``ok`` alone. A status with nothing else to say still gets a line that
names the outcome, and an unrecognised status renders the status itself
rather than borrowing a neighbour's meaning.

Only the flat-string wrappers use this seam. ``propose_decision`` returns the
envelope itself on every status, so it has nothing to flatten.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from nauro.store.post_commit import with_assessment

OK_STATUS = "ok"

# The line for a status whose envelope carries no explanatory text of its own.
# Every one of them says what happened to the write, because that is the fact
# the flat string exists to carry.
_STATUS_FALLBACKS: dict[str, str] = {
    "rejected": "The write was rejected. Nothing was recorded.",
    "noop": "No write was made. The store reported nothing to update.",
    "error": "The write did not run. Nothing was recorded.",
}

_UNKNOWN_STATUS = "Unexpected write status {status!r}. Treat the write as unconfirmed."


class EnvelopeError(BaseModel):
    """The ``error`` block of a write envelope, read for its prose.

    Deliberately looser than ``nauro_core.operations.ErrorPayload``, which
    forbids extra keys and requires ``kind``. This is a presentation
    boundary: a core release that adds a field to the error payload must not
    turn a renderable envelope into an exception here.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    reason: str | None = None
    guidance: str | None = None


class WriteEnvelope(BaseModel):
    """The status-bearing fields of a local write envelope.

    Parsed at the transport boundary so the dispatch reads typed attributes
    instead of walking the raw dict. Extra keys are ignored on purpose: each
    write tool carries its own payload (``warning``, ``hint``, ``project``)
    that the status dispatch has no business knowing about.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: str = OK_STATUS
    error: EnvelopeError | None = None
    guidance: str | None = None

    @property
    def detail(self) -> str | None:
        """The envelope's own account of the outcome, if it carries one.

        A rejection puts it in ``error.reason``; a store-missing failure puts
        it in the top-level ``guidance``.
        """
        if self.error is not None and self.error.reason:
            return self.error.reason
        return self.guidance or None

    def line(self, success: Callable[[], str]) -> str:
        """The one line this envelope's status warrants."""
        if self.status == OK_STATUS:
            return success()
        fallback = _STATUS_FALLBACKS.get(self.status)
        if fallback is not None:
            return self.detail or fallback
        unknown = _UNKNOWN_STATUS.format(status=self.status)
        return f"{unknown} {self.detail}" if self.detail else unknown


def render_write_status(envelope: dict, success: Callable[[], str]) -> str:
    """Render *envelope* as one line, calling *success* only on ``ok``.

    Args:
        envelope: The dict a write adapter in ``nauro.mcp.tools`` returned.
        success: Builds the wrapper's own confirmation line. Called on the
            ``ok`` status and on no other, so a wrapper cannot report a
            write that did not happen.

    Returns:
        The line for the envelope's status, carrying any post-commit
        assessment the envelope holds. A write that did not commit has no
        assessment to carry, so the two compose without a special case.
    """
    return with_assessment(WriteEnvelope.model_validate(envelope).line(success), envelope)
