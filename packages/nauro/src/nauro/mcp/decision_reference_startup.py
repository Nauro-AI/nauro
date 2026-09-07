"""Start an explicit decision-only stdio connection without local store startup."""

from pathlib import Path

import httpx

from nauro.mcp.decision_reference import reference_server
from nauro.sync.decision_profile import load_reference_profile, profile_transport
from nauro.sync.decision_reference import DecisionReferenceError


class ReferenceStartupError(Exception):
    pass


def run_reference_stdio(path: Path) -> None:
    with httpx.Client() as client:
        try:
            profile = load_reference_profile(path)
            transport = profile_transport(profile, client)
            transport.initialize()
        except (ValueError, OSError, DecisionReferenceError):
            raise ReferenceStartupError(
                "Could not start the reference connection. "
                "Check the profile, credentials and server."
            ) from None
        reference_server(transport).run(transport="stdio")
