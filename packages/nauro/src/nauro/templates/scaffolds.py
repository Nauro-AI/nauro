"""Project store scaffold templates for `nauro init`.

Convention: no Jinja2 — use f-strings and string templates only.
These templates define the initial file contents created when a new
Nauro project store is initialized at ~/.nauro/projects/<name>/.

Bracketed [prompts] guide the user on what to fill in.

Decision files (the first-decision scaffold) are emitted via
``nauro_core.decision_model.format_decision`` rather than a string
template, so the one source of truth for the on-disk decision format stays
in nauro-core.
"""

from datetime import datetime, timezone
from pathlib import Path

from nauro_core.constants import PROJECT_MD_SCAFFOLD_BODY
from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionStatus,
    RejectedAlternative,
    format_decision,
)

from nauro import constants as C  # noqa: N812

# Composed from the shared body constant so this template and the kernel's
# scaffold-form guard (nauro_core.parsing.is_scaffold_project_md, which lets
# build_l0 skip unedited scaffolds) can never drift apart.
PROJECT_MD = "# {project_name}\n" + PROJECT_MD_SCAFFOLD_BODY

STATE_CURRENT_MD = """\
# Current State

_(No state recorded yet.)_
"""

STACK_MD = """\
# Stack
## Language & Framework
**Python + Typer** *(example - replace with your choice)* \
- Chosen for: fast CLI prototyping, strong ecosystem for LLM tooling. \
Rejected: Go (faster binary, but slower iteration for a solo developer), \
Node/oclif (weaker subprocess and file handling).
[Replace the example above and add your core choices \
using the same format: **Choice** - Chosen for: reasons. \
Rejected: alternatives (why not).]
## Infrastructure
[e.g. "**SQLite** - Chosen for: zero-config, single-file, \
good enough for local-first v1. \
Rejected: Postgres (operational overhead for a CLI tool)."]
## Key Libraries
[e.g. "**FastAPI** for MCP server - async, auto-generated OpenAPI docs, familiar."]
"""

OPEN_QUESTIONS_MD = """\
# Open Questions
- [First unresolved question, e.g. "Should we support team sync in v1 or defer to v2?"]
- ~~[Example resolved question]~~ → Resolved: [How it was resolved]
"""

_FIRST_DECISION_RATIONALE = (
    "Initial project setup - scaffold the Nauro project store and begin "
    "tracking architectural decisions.\n\n"
    "Explicit decision tracking from day one prevents context loss when "
    "onboarding contributors or switching between projects."
)

_FIRST_DECISION_REJECTED = (
    RejectedAlternative(
        name="Ad-hoc notes in README",
        reason="Hard to find, no structure - does not scale past a few entries.",
    ),
    RejectedAlternative(
        name="No tracking until later",
        reason="Context is already lost by the time you decide you need it.",
    ),
)


def _build_first_decision(date_str: str) -> str:
    """Emit the scaffolded first decision in v2 format."""
    decision = Decision(
        date=datetime.strptime(date_str, "%Y-%m-%d").date(),
        version=1,
        status=DecisionStatus.active,
        confidence=DecisionConfidence.high,
        num=1,
        title="Initial project setup",
        rationale=_FIRST_DECISION_RATIONALE,
        rejected=list(_FIRST_DECISION_REJECTED),
    )
    return format_decision(decision)


def scaffold_project_store(project_name: str, store_path: Path) -> None:
    """Write all template files to the project store directory.

    Creates: project.md, state_current.md, stack.md, open-questions.md,
    decisions/ directory (with 001-initial-setup.md), snapshots/ directory.

    Args:
        project_name: Name of the project.
        store_path: Path to the project store (e.g. ~/.nauro/projects/<name>/).
    """
    store_path.mkdir(parents=True, exist_ok=True)
    (store_path / C.DECISIONS_DIR).mkdir(exist_ok=True)
    (store_path / C.SNAPSHOTS_DIR).mkdir(exist_ok=True)

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    (store_path / C.PROJECT_MD).write_text(
        render_scaffold(PROJECT_MD, project_name=project_name), encoding="utf-8"
    )
    (store_path / C.STATE_CURRENT_FILENAME).write_text(
        render_scaffold(STATE_CURRENT_MD), encoding="utf-8"
    )
    (store_path / C.STACK_MD).write_text(render_scaffold(STACK_MD), encoding="utf-8")
    (store_path / C.OPEN_QUESTIONS_MD).write_text(
        render_scaffold(OPEN_QUESTIONS_MD), encoding="utf-8"
    )

    # Scaffold the first decision as a teaching example (v2 format via nauro-core).
    (store_path / C.DECISIONS_DIR / "001-initial-setup.md").write_text(
        _build_first_decision(created_at), encoding="utf-8"
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
