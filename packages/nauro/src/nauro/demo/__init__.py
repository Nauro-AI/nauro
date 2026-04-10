"""Demo project templates for `nauro init --demo`.

Creates a sample project with pre-written decisions, state, questions,
and a snapshot — so users can explore Nauro's features immediately.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from nauro import constants

# ── Decision files (matching writer.py's exact format) ──

_DEMO_DATE = "2026-03-15"

DECISION_001 = f"""\
# 001 — Chose PostgreSQL over MongoDB for ACID compliance

**Date:** {_DEMO_DATE}
**Version:** 1
**Status:** active
**Confidence:** high
**Type:** infrastructure
**Reversibility:** hard
**Source:** manual

## Decision

PostgreSQL was chosen as the primary database for its strong ACID compliance,
mature ecosystem, and excellent support for complex queries. Data integrity is
critical for a task management system where users rely on accurate state
transitions and audit trails.

## Rejected Alternatives

### MongoDB
Eventual consistency model is unsuitable for task state transitions where
users expect immediate consistency. The flexible schema is appealing but
our data model is well-defined and benefits from strict typing.
"""

DECISION_002 = f"""\
# 002 — REST API over GraphQL for simplicity

**Date:** {_DEMO_DATE}
**Version:** 1
**Status:** active
**Confidence:** medium
**Type:** api_design
**Reversibility:** moderate
**Source:** manual

## Decision

REST was chosen over GraphQL for the API layer. The team has deep REST
experience and our resource model maps naturally to REST endpoints. For
a v1 with a small frontend team, the added complexity of a GraphQL schema,
resolvers, and client-side caching is not justified.

## Rejected Alternatives

### GraphQL
Would provide more flexible querying and reduce over-fetching, but adds
significant complexity: schema management, resolver implementation, and
requires a more sophisticated client. Reconsidering for v2 if the frontend
needs grow beyond simple CRUD.
"""

DECISION_003 = f"""\
# 003 — Monorepo with Turborepo over polyrepo

**Date:** {_DEMO_DATE}
**Version:** 1
**Status:** active
**Confidence:** high
**Type:** architecture
**Reversibility:** moderate
**Source:** manual

## Decision

All services (API, web frontend, shared libraries) live in a single monorepo
managed by Turborepo. This simplifies cross-package changes, ensures consistent
tooling, and makes CI/CD pipelines straightforward. Shared TypeScript types
between frontend and backend are a major win.

## Rejected Alternatives

### Polyrepo (separate repos per service)
Dependency management overhead is significant with only 2-3 developers.
Version pinning across repos, coordinating releases, and keeping shared
types in sync would slow us down considerably at this team size.
"""

STATE_CURRENT_MD = f"""\
# Current State

Implementing user authentication \u2014 building JWT-based auth flow \
with refresh tokens and RBAC.

*Last updated: {_DEMO_DATE}T12:00Z*
"""

OPEN_QUESTIONS_MD = """\
# Open Questions
- [2026-03-14 10:00 UTC] Should we add WebSocket support for real-time updates?
- [2026-03-13 15:30 UTC] Redis vs in-memory caching for session storage?
"""

PROJECT_MD = """\
# TaskFlow

**One-liner:** A task management API for teams that need structured workflows.

## Goals
- Ship a production-ready REST API with auth, task CRUD, and team management
- Support workflow automation (task state transitions, notifications)

## Non-goals
- Not a project management tool — no Gantt charts, resource planning, or budgeting
- No mobile app in v1 — API-first, web frontend only

## Users
Small engineering teams (3-10 people) who need a lightweight task tracker
with API access for integrations. Primary users are developers who want
to automate task workflows via the API.

## Constraints
- Must ship MVP by June 2026
- PostgreSQL for persistence (ACID required for task state transitions)
- Deploy on AWS (team has existing infrastructure)
"""

STACK_MD = """\
# Stack

## Language & Framework
- **TypeScript + Express** — Chosen for: team familiarity, strong typing across \
frontend and backend. Rejected: Go (faster runtime, but slower iteration), \
Python/FastAPI (weaker typing story for shared models).

## Database
- **PostgreSQL 16** — Chosen for: ACID compliance, mature tooling, excellent \
JSON support for flexible metadata. Rejected: MongoDB (eventual consistency \
unsuitable for task state), SQLite (no concurrent write support for multi-user).

## Infrastructure
- **AWS ECS Fargate** — Chosen for: managed containers without server maintenance, \
auto-scaling. Rejected: Lambda (cold starts hurt UX for API), EC2 (operational overhead).

## Key Libraries
- **Prisma** for ORM — type-safe database access, excellent migration tooling
- **Passport.js** for auth — battle-tested, supports multiple strategies
- **Turborepo** for monorepo management — fast builds, smart caching
"""


def create_demo_project(store_path: Path) -> None:
    """Write all demo project files to the store directory.

    Creates the same structure as a real project: project.md, state.md,
    stack.md, open-questions.md, 3 decisions, and a snapshot.

    Args:
        store_path: Path to the project store directory.
    """
    store_path.mkdir(parents=True, exist_ok=True)
    decisions_dir = store_path / constants.DECISIONS_DIR
    decisions_dir.mkdir(exist_ok=True)
    snapshots_dir = store_path / constants.SNAPSHOTS_DIR
    snapshots_dir.mkdir(exist_ok=True)

    # Write content files
    (store_path / constants.PROJECT_MD).write_text(PROJECT_MD)
    (store_path / constants.STATE_CURRENT_FILENAME).write_text(STATE_CURRENT_MD)
    (store_path / constants.STACK_MD).write_text(STACK_MD)
    (store_path / constants.OPEN_QUESTIONS_MD).write_text(OPEN_QUESTIONS_MD)

    # Write decisions
    (decisions_dir / "001-chose-postgresql-over-mongodb.md").write_text(DECISION_001)
    (decisions_dir / "002-rest-api-over-graphql.md").write_text(DECISION_002)
    (decisions_dir / "003-monorepo-with-turborepo.md").write_text(DECISION_003)

    # Write a snapshot so diff_since_last_session returns something
    files = {
        constants.PROJECT_MD: PROJECT_MD,
        constants.STATE_CURRENT_FILENAME: STATE_CURRENT_MD,
        constants.STACK_MD: STACK_MD,
        constants.OPEN_QUESTIONS_MD: OPEN_QUESTIONS_MD,
        f"{constants.DECISIONS_DIR}/001-chose-postgresql-over-mongodb.md": DECISION_001,
        f"{constants.DECISIONS_DIR}/002-rest-api-over-graphql.md": DECISION_002,
        f"{constants.DECISIONS_DIR}/003-monorepo-with-turborepo.md": DECISION_003,
    }

    snapshot = {
        "schema_version": 1,
        "version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "trigger": "demo project created",
        "trigger_detail": "",
        "token_count": sum(len(v) for v in files.values()) // 4,
        "files": files,
    }

    (snapshots_dir / "v001.json").write_text(json.dumps(snapshot, indent=2) + "\n")
