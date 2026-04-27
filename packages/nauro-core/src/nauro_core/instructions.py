"""Per-user composition of MCP server instructions.

The static instructional block (MCP_INSTRUCTIONS_STATIC) is project-agnostic.
Remote callers append a small section listing the user's projects so Claude
can pick a project_id without a discovery roundtrip. For users with no
projects, WELCOME_NO_PROJECT replaces the project list with onboarding copy.
"""

from __future__ import annotations

MAX_INLINE_PROJECTS = 3

WELCOME_NO_PROJECT = (
    "Welcome to Nauro. You have no projects yet.\n"
    "\n"
    "Next steps:\n"
    "1. Run `nauro auth login` (required before any cloud operation).\n"
    "2. Create a project:\n"
    "   - `nauro init <name>` for a local-only project (no network).\n"
    "   - `nauro init --cloud <name>` to create a server-minted cloud project.\n"
    "3. Or, if a teammate already created a cloud project, run "
    "`nauro attach <project_id>` to connect this machine to it.\n"
    "\n"
    "Once a project exists, every tool call (except list_projects) requires "
    "its project_id."
)


def build_remote_instructions(
    static_block: str,
    projects: list[dict],
) -> str:
    """Combine static instructions with a per-user project section.

    `projects` is a list of dicts each with at least 'project_id' (str, ULID)
    and 'name' (str).

    - Empty list: append WELCOME_NO_PROJECT.
    - 1..MAX_INLINE_PROJECTS: append "Your projects:" with each rendered as
      `name — {project_id[:8]}`, sorted by (name.lower(), project_id).
    - More than MAX_INLINE_PROJECTS: append a count + hint to call
      list_projects, without enumerating each one.
    """
    if not projects:
        return f"{static_block}\n\n{WELCOME_NO_PROJECT}"

    if len(projects) <= MAX_INLINE_PROJECTS:
        ordered = sorted(
            projects,
            key=lambda p: (p["name"].lower(), p["project_id"]),
        )
        lines = ["Your projects:"]
        for p in ordered:
            lines.append(f"- {p['name']} — {p['project_id'][:8]}")
        lines.append(
            "\nPass the matching project_id to every tool call "
            "(except list_projects)."
        )
        return f"{static_block}\n\n" + "\n".join(lines)

    return (
        f"{static_block}\n\n"
        f"You have {len(projects)} projects. "
        "Call list_projects to see them all and pick a project_id; "
        "every tool call except list_projects requires one."
    )
