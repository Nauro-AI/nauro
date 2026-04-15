"""Onboarding and empty-state guidance text for the local MCP server.

These constants are returned when a user has no project store or when a
project exists but has no decisions/snapshots yet.  They give the LLM
enough context to guide the user toward a productive first experience.
"""

WELCOME_NO_PROJECT = (
    "Welcome to Nauro! No project store found.\n"
    "\n"
    "To get started:\n"
    "1. Run: nauro init <project-name>\n"
    "2. Make some commits, then: nauro extract\n"
    '3. Or log a decision directly: nauro note "Chose Postgres over MongoDB"\n'
    "\n"
    "Your decisions will then be available here and across all connected AI tools."
)

NO_DECISIONS_YET = (
    "This project has no decisions yet.\n"
    "\n"
    "To add your first decision:\n"
    "- Run: nauro extract (extracts from recent git commits)\n"
    '- Run: nauro note "Your decision here"\n'
    "- Or use propose_decision right here in this conversation"
)

NO_SNAPSHOTS_YET = (
    "No snapshots available yet.\n"
    "\n"
    "Snapshots are created automatically when decisions are written "
    "or state is updated. Use propose_decision or update_state to get started."
)

NO_DECISIONS_TO_CHECK = (
    "No existing decisions to check against.\n"
    "\n"
    "Use propose_decision to record your first architectural decision, "
    "then check_decision can help verify new approaches against "
    "your recorded decisions."
)

NO_CONTEXT_YET = (
    "This project has no context data yet.\n"
    "\n"
    "To populate your project context:\n"
    "- Use propose_decision to record architectural decisions\n"
    "- Use update_state to track current progress\n"
    "- Run: nauro extract to pull decisions from git history"
)
