"""Guidance text for a missing or empty project store.

Store resolution and the MCP tools return these constants when no store is
registered or the store holds no decisions yet. ``NO_DECISIONS_TO_CHECK`` is
re-exported from ``nauro_core.constants`` so local and hosted transports
render the same empty-store text.
"""

from nauro_core.constants import NO_DECISIONS_TO_CHECK as NO_DECISIONS_TO_CHECK
from nauro_core.protocol import APPROVAL_BEFORE_PROPOSE

WELCOME_NO_PROJECT = (
    "Welcome to Nauro! No project store found.\n"
    "\n"
    "Nauro is local-first: this server reads the store at ~/.nauro on this\n"
    "machine, resolved from the current working directory.\n"
    "\n"
    "To get started:\n"
    "1. From an existing repo, run: nauro adopt\n"
    "   (for a project without a repo: nauro init <project-name>)\n"
    '2. Log a decision: nauro note "Chose Postgres over MongoDB"\n'
    "\n"
    "Your decisions will then be available here and across all connected AI tools."
)

NO_CONTEXT_YET = (
    "This project has no context data yet.\n"
    "\n"
    "To populate your project context:\n"
    "- Use propose_decision to record architectural decisions\n"
    "- Use update_state to track current progress\n"
    "\n"
    f"{APPROVAL_BEFORE_PROPOSE}"
)


def disconnected_project_guidance(reason_code: str, mode: str) -> str:
    """Return the approved user copy for a disconnected project state."""
    if reason_code == "not_connected_on_this_machine" and mode == "cloud":
        return (
            "This cloud project has not been connected on this machine. "
            "Run `nauro reconnect` to verify access and restore its latest synced record."
        )
    if reason_code == "not_connected_on_this_machine":
        return (
            "This repository names a local Nauro project that has not been connected on this "
            "machine. If you have the project record, run `nauro reconnect` and locate it. "
            "Otherwise, the record remains on the machine where the project was adopted.\n\n"
            "The project owner can run `nauro link --cloud`, commit the updated project config, "
            "and push it. After pulling that change, other machines can attach and restore the "
            "cloud record."
        )
    if reason_code == "connected_record_missing":
        return (
            "Nauro was connected on this machine, but the local project record is no longer at "
            "its registered location. Run `nauro reconnect` to locate it or restore an eligible "
            "cloud copy."
        )
    if reason_code == "connected_record_invalid":
        return (
            "Nauro found the registered project record, but it could not validate it. Nauro will "
            "not replace or repair it automatically. Run `nauro reconnect` to inspect the record "
            "and available recovery options."
        )
    if reason_code == "connected_binding_conflict":
        return (
            "Nauro found conflicting local records for this project. It will not choose or "
            "overwrite either. Run `nauro reconnect` to inspect the conflict and available "
            "recovery options."
        )
    raise ValueError(f"Unknown disconnected-project reason: {reason_code!r}.")
