"""Per-user composition of MCP server instructions.

The static instructional block (MCP_INSTRUCTIONS_STATIC) is project-agnostic.
Remote callers prepend a per-user section that depends on how many projects
the caller has access to:

- 0 projects: WELCOME_NO_PROJECT onboarding copy.
- 1 project: a single orientation line naming the project — no project_id
  rendered, since tools auto-resolve to it.
- 2+ projects: a disambiguation list with full ULIDs (or a list_projects
  pointer when the count exceeds MAX_INLINE_PROJECTS).
"""

from __future__ import annotations

from typing import TypedDict


# Module-private (not in ``__all__``); annotates the existing ``projects`` input
# shape only, no behavior change.
class ProjectRef(TypedDict):
    """A project reference passed to ``build_remote_instructions``."""

    project_id: str
    name: str


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
    projects: list[ProjectRef],
) -> str:
    """Combine static instructions with a per-user project section.

    `projects` is a list of dicts each with at least 'project_id' (str, ULID)
    and 'name' (str).

    The per-user section is prepended before ``static_block`` so it survives
    client-side truncation of the MCP ``initialize.instructions`` field. The
    project section is the load-bearing payload for multi-project users (it
    carries the project_id they must pass); the static block is repeated
    project-agnostic guidance the agent can recover from other surfaces.

    - Empty list: prepend WELCOME_NO_PROJECT.
    - Exactly 1 project: prepend one orientation line with the project name.
      No project_id is rendered — tools auto-resolve to the only project.
    - 2..MAX_INLINE_PROJECTS: prepend a "You have N projects:" list with each
      project rendered as `name — {full project_id}`, sorted by
      (name.lower(), project_id). Tools require an explicit project_id when
      multiple exist.
    - More than MAX_INLINE_PROJECTS: prepend a count + hint to call
      list_projects, without enumerating each one.
    """
    if not projects:
        return f"{WELCOME_NO_PROJECT}\n\n{static_block}"

    if len(projects) == 1:
        name = projects[0]["name"]
        return (
            f"Connected to project '{name}' — tools auto-resolve, "
            "you do not need to pass a project_id."
            f"\n\n{static_block}"
        )

    if len(projects) <= MAX_INLINE_PROJECTS:
        ordered = sorted(
            projects,
            key=lambda p: (p["name"].lower(), p["project_id"]),
        )
        lines = [f"You have {len(projects)} projects:"]
        for p in ordered:
            lines.append(f"- {p['name']} — {p['project_id']}")
        lines.append(
            "\nTools require an explicit project_id when multiple exist; "
            "pass one of the IDs above."
        )
        return "\n".join(lines) + f"\n\n{static_block}"

    return (
        f"You have {len(projects)} projects. "
        "Tools require an explicit project_id when multiple exist — "
        "call list_projects to see them all and pick one."
        f"\n\n{static_block}"
    )
