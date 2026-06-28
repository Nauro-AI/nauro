"""Onboarding and empty-state guidance text for the local MCP server.

These constants are returned when a user has no project store or when a
project exists but has no decisions/snapshots yet.  They give the LLM
enough context to guide the user toward a productive first experience.

``NO_DECISIONS_TO_CHECK`` is the shared cross-surface string and lives in
``nauro_core.constants`` so the same empty-store text reaches users on
local and cloud transports. The re-export here preserves the import path
existing callers use.
"""

from nauro_core.constants import NO_DECISIONS_TO_CHECK as NO_DECISIONS_TO_CHECK

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
    "- Use update_state to track current progress"
)
