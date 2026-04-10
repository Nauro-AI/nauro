"""Project store scaffold templates for `nauro init`.

Convention: no Jinja2 — use f-strings and string templates only.
These templates define the initial file contents created when a new
Nauro project store is initialized at ~/.nauro/projects/<name>/.

Bracketed [prompts] guide the user on what to fill in.
"""

from datetime import UTC, datetime
from pathlib import Path

from nauro import constants as C  # noqa: N812

PROJECT_MD = """\
# {project_name}
**One-liner:** [What this does in one sentence, e.g. \
"A CLI tool that syncs project context to AI coding agents."]
## Goals
- [Primary goal — what success looks like in concrete terms, \
e.g. "Reduce agent ramp-up from 5 min to under 30 seconds"]
- [Secondary goal]
## Non-goals
- [Something explicitly out of scope, \
e.g. "Not a project management tool — no task tracking"]
## Users
[Who uses this and how — be specific. \
"Mobile-first consumers aged 18-35 discovering recipes" \
is useful. "Users" is not.]
## Constraints
- [Hard limits: budget, timeline, regulatory, platform, e.g. "Must ship MVP by June 2026"]
- [Technical constraints, e.g. "Must run offline — no cloud dependency in v1"]
"""

STATE_MD = """\
# State

## Current
[What you're working on right now — e.g. "Building user auth flow, blocked on Stripe API approval"]

## History
"""

STACK_MD = """\
# Stack
## Language & Framework
**Python + Typer** *(example — replace with your choice)* \
— Chosen for: fast CLI prototyping, strong ecosystem for LLM tooling. \
Rejected: Go (faster binary, but slower iteration for a solo developer), \
Node/oclif (weaker subprocess and file handling).
[Replace the example above and add your core choices \
using the same format: **Choice** — Chosen for: reasons. \
Rejected: alternatives (why not).]
## Infrastructure
[e.g. "**SQLite** — Chosen for: zero-config, single-file, \
good enough for local-first v1. \
Rejected: Postgres (operational overhead for a CLI tool)."]
## Key Libraries
[e.g. "**FastAPI** for MCP server — async, auto-generated OpenAPI docs, familiar."]
"""

OPEN_QUESTIONS_MD = """\
# Open Questions
- [First unresolved question, e.g. "Should we support team sync in v1 or defer to v2?"]
- ~~[Example resolved question]~~ → Resolved: [How it was resolved]
"""

DECISION_TEMPLATE = """\
---
date: {date}
status: accepted
confidence: {confidence}
---

# {number}: {title}

## Context
[What situation or problem prompted this decision]

## Decision
{title}

{rationale_section}\
{rejected_section}\
"""

FIRST_DECISION_MD = """\
---
date: {date}
status: accepted
confidence: high
---

# 001: Initial project setup

## Context
Project store initialized. This first decision documents the project
bootstrapping choices so future decisions can reference it.

## Decision
Initial project setup — scaffold the Nauro project store and begin
tracking architectural decisions.

## Rationale
Explicit decision tracking from day one prevents context loss when
onboarding contributors or switching between projects.

## Rejected Alternatives
- Ad-hoc notes in README (hard to find, no structure)
- No tracking until later (context already lost by then)
"""


def get_scaffolds() -> dict[str, str]:
    """Return all scaffold templates as a dict keyed by filename.

    Returns:
        Dict mapping filenames to their template strings.
    """
    return {
        C.PROJECT_MD: PROJECT_MD,
        C.STATE_MD: STATE_MD,
        C.STACK_MD: STACK_MD,
        C.OPEN_QUESTIONS_MD: OPEN_QUESTIONS_MD,
    }


def scaffold_project_store(project_name: str, store_path: Path) -> None:
    """Write all template files to the project store directory.

    Creates: project.md, state.md, stack.md, open-questions.md,
    decisions/ directory (with 001-initial-setup.md), snapshots/ directory.

    Args:
        project_name: Name of the project.
        store_path: Path to the project store (e.g. ~/.nauro/projects/<name>/).
    """
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / C.DECISIONS_DIR).mkdir(exist_ok=True)
    (store_path / C.SNAPSHOTS_DIR).mkdir(exist_ok=True)

    created_at = datetime.now(UTC).strftime("%Y-%m-%d")

    (store_path / C.PROJECT_MD).write_text(render_scaffold(PROJECT_MD, project_name=project_name))
    (store_path / C.STATE_MD).write_text(render_scaffold(STATE_MD))
    (store_path / C.STACK_MD).write_text(render_scaffold(STACK_MD))
    (store_path / C.OPEN_QUESTIONS_MD).write_text(render_scaffold(OPEN_QUESTIONS_MD))

    # Scaffold the first decision as a teaching example
    (store_path / C.DECISIONS_DIR / "001-initial-setup.md").write_text(
        render_scaffold(FIRST_DECISION_MD, date=created_at)
    )


def render_scaffold(template: str, **kwargs: str) -> str:
    """Render a scaffold template with the given variables.

    Args:
        template: One of the template strings above.
        **kwargs: Template variable values.

    Returns:
        Rendered template string.
    """
    return template.format(**kwargs)
