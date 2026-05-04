"""Per-user composition of MCP server instructions.

The static instructional block (MCP_INSTRUCTIONS_STATIC) is project-agnostic.
Remote callers append a small section listing the user's projects so Claude
knows which projects exist. Tools auto-resolve to the user's project when
they have only one; the per-user section is informational + disambiguation
hint when they have multiple. For users with no projects, WELCOME_NO_PROJECT
replaces the project list with onboarding copy.
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
    "Once a project exists, tools auto-resolve to it — you do not need to "
    "pass a project_id."
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
      `name — {full project_id}`, sorted by (name.lower(), project_id).
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
            lines.append(f"- {p['name']} — {p['project_id']}")
        lines.append(
            "\nTools resolve to your project automatically when you have one; "
            "pass an explicit project_id only if you have multiple."
        )
        return f"{static_block}\n\n" + "\n".join(lines)

    return (
        f"{static_block}\n\n"
        f"You have {len(projects)} projects. "
        "Tools require an explicit project_id when multiple exist — "
        "call list_projects to see them all and pick one."
    )
