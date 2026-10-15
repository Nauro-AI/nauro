from __future__ import annotations

from nauro_core.operations import ErrorPayload

from nauro.onboarding import WELCOME_NO_PROJECT
from nauro.store.resolution import (
    DisconnectedProjectError,
    NoProjectError,
    StoreResolutionError,
)


def guidance_error(guidance: str) -> dict[str, object]:
    """Return the local error envelope that carries only guidance text."""
    return {"store": "local", "status": "error", "guidance": guidance}


def resolution_error_envelope(error: StoreResolutionError) -> dict[str, object]:
    if isinstance(error, NoProjectError):
        return guidance_error(WELCOME_NO_PROJECT)
    if isinstance(error, DisconnectedProjectError):
        state = error.state
        return {
            "store": "local",
            "status": "error",
            "error": ErrorPayload(kind="error", reason=state.guidance).model_dump(
                exclude_none=True
            ),
            "guidance": state.guidance,
            "project_id": state.project_id,
            "project_name": state.display_name,
            "project_mode": state.mode,
            "reason_code": state.reason_code,
            "recovery_actions": list(state.recovery_actions),
        }
    return guidance_error(str(error))
