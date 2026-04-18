"""Demo project templates for `nauro init --demo`.

Creates a sample project with pre-written decisions, state, questions,
and a snapshot — so users can explore Nauro's features immediately.

Decision files are built as ``Decision`` objects and serialized via
``format_decision_v2`` so the demo output is guaranteed to match whatever
real writer output looks like (no template drift).
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision_v2,
)

from nauro import constants

_DEMO_DATE = date(2026, 3, 15)


def _decision(
    num: int,
    title: str,
    confidence: DecisionConfidence,
    decision_type: DecisionType,
    reversibility: Reversibility,
    rationale: str,
    rejected: list[RejectedAlternative],
) -> Decision:
    return Decision(
        date=_DEMO_DATE,
        version=1,
        status=DecisionStatus.active,
        confidence=confidence,
        decision_type=decision_type,
        reversibility=reversibility,
        source=DecisionSource.manual,
        num=num,
        title=title,
        rationale=rationale,
        rejected=rejected,
    )


# ── 7 demo decisions — same prose as the pre-0.2 demo, emitted as v2 ──

DEMO_DECISIONS: list[Decision] = [
    _decision(
        1,
        "Chose PostgreSQL over MongoDB for ACID compliance",
        DecisionConfidence.high,
        DecisionType.infrastructure,
        Reversibility.hard,
        "PostgreSQL was chosen as the primary database for its strong ACID compliance, "
        "mature ecosystem, and excellent support for complex queries. Data integrity is "
        "critical for a task management system where users rely on accurate state "
        "transitions and audit trails.",
        [
            RejectedAlternative(
                name="MongoDB",
                reason=(
                    "Eventual consistency model is unsuitable for task state transitions where "
                    "users expect immediate consistency. The flexible schema is appealing but "
                    "our data model is well-defined and benefits from strict typing."
                ),
            ),
        ],
    ),
    _decision(
        2,
        "REST API over GraphQL for simplicity",
        DecisionConfidence.medium,
        DecisionType.api_design,
        Reversibility.moderate,
        "REST was chosen over GraphQL for the API layer. The team has deep REST "
        "experience and our resource model maps naturally to REST endpoints. For "
        "a v1 with a small frontend team, the added complexity of a GraphQL schema, "
        "resolvers, and client-side caching is not justified.",
        [
            RejectedAlternative(
                name="GraphQL",
                reason=(
                    "Would provide more flexible querying and reduce over-fetching, but adds "
                    "significant complexity: schema management, resolver implementation, and "
                    "requires a more sophisticated client. Reconsidering for v2 if the frontend "
                    "needs grow beyond simple CRUD."
                ),
            ),
        ],
    ),
    _decision(
        3,
        "Monorepo with Turborepo over polyrepo",
        DecisionConfidence.high,
        DecisionType.architecture,
        Reversibility.moderate,
        "All services (API, web frontend, shared libraries) live in a single monorepo "
        "managed by Turborepo. This simplifies cross-package changes, ensures consistent "
        "tooling, and makes CI/CD pipelines straightforward. Shared TypeScript types "
        "between frontend and backend are a major win.",
        [
            RejectedAlternative(
                name="Polyrepo (separate repos per service)",
                reason=(
                    "Dependency management overhead is significant with only 2-3 developers. "
                    "Version pinning across repos, coordinating releases, and keeping shared "
                    "types in sync would slow us down considerably at this team size."
                ),
            ),
        ],
    ),
    _decision(
        4,
        "SSE over WebSocket for live task updates",
        DecisionConfidence.high,
        DecisionType.api_design,
        Reversibility.moderate,
        "Server-Sent Events (SSE) for pushing live task updates to the frontend. "
        "SSE uses standard HTTP, reconnects automatically on disconnect, and works "
        "through every proxy and load balancer without configuration. During ECS "
        "rolling deploys, WebSocket connections were not released cleanly — new "
        "tasks routed to draining containers, causing 30-second stalls until "
        "timeout. SSE clients reconnect to healthy targets within 3 seconds.",
        [
            RejectedAlternative(
                name="WebSocket",
                reason=(
                    "Persistent connections were not released during ECS rolling deploys, "
                    "causing connection storms when multiple containers drained simultaneously. "
                    "Debugging required custom connection-tracking middleware. The bidirectional "
                    "channel is unnecessary — clients never push data through the event stream."
                ),
            ),
        ],
    ),
    _decision(
        5,
        "All processing in request path, no background workers",
        DecisionConfidence.medium,
        DecisionType.architecture,
        Reversibility.moderate,
        "All task processing (notifications, state transitions, webhook deliveries) "
        "happens synchronously in the request path. No job queue, no worker "
        "processes. p99 API latency is under 200ms with this approach, and the "
        "operational surface stays small: one container type, one log stream, "
        "one failure mode.",
        [
            RejectedAlternative(
                name="Background job queue (Redis + Bull / SQS)",
                reason=(
                    "Added three failure modes the team couldn't monitor in v1: stuck jobs, "
                    "duplicate delivery on retry, and silent queue backup when the worker "
                    "fell behind. For current throughput (~50 req/s peak), synchronous "
                    "processing is fast enough and dramatically simpler to debug."
                ),
            ),
        ],
    ),
    _decision(
        6,
        "Cursor-based pagination, not offset",
        DecisionConfidence.high,
        DecisionType.api_design,
        Reversibility.hard,
        "All list endpoints use cursor-based pagination with opaque encoded cursors. "
        "Offset pagination breaks when items are inserted or deleted between pages — "
        "users see duplicates or miss items entirely. Cursor pagination provides "
        "stable iteration regardless of concurrent writes, which matters for a "
        "multi-user task board where tasks move between states constantly.",
        [
            RejectedAlternative(
                name="LIMIT/OFFSET",
                reason=(
                    "Simple to implement but produces inconsistent results under concurrent "
                    "writes. With 50+ active users modifying task state, offset drift caused "
                    "visible duplicates in the frontend during testing. Also degrades at scale: "
                    "OFFSET 10000 still scans and discards 10,000 rows."
                ),
            ),
        ],
    ),
    _decision(
        7,
        "Hard delete with audit log, no soft deletes",
        DecisionConfidence.high,
        DecisionType.architecture,
        Reversibility.hard,
        "Deleted tasks are removed from the tasks table and a record is written to "
        "the audit_events table. No soft deletes. The audit log captures who deleted "
        "what and when, satisfying compliance requirements without polluting the "
        "primary table.",
        [
            RejectedAlternative(
                name="Soft deletes (deleted_at column)",
                reason=(
                    "Leaks into every query: every WHERE clause, every index, every JOIN needs "
                    "to filter on deleted_at IS NULL. In testing, three bugs shipped because "
                    "a query forgot the filter and showed deleted tasks in the UI. The audit "
                    "log table provides the same compliance trail without the query tax."
                ),
            ),
        ],
    ),
]


STATE_CURRENT_MD = f"""\
# Current State

Implementing user authentication \u2014 building JWT-based auth flow \
with refresh tokens and RBAC.

*Last updated: {_DEMO_DATE.isoformat()}T12:00Z*
"""

OPEN_QUESTIONS_MD = """\
# Open Questions
- [2026-03-14 10:00 UTC] Should we add rate limiting at the API gateway or application layer?
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


def _decision_filename(decision: Decision, slug: str) -> str:
    return f"{decision.num:03d}-{slug}.md"


_DEMO_SLUGS = {
    1: "chose-postgresql-over-mongodb",
    2: "rest-api-over-graphql",
    3: "monorepo-with-turborepo",
    4: "sse-over-websocket",
    5: "no-background-workers",
    6: "cursor-based-pagination",
    7: "hard-delete-with-audit-log",
}


def create_demo_project(store_path: Path) -> None:
    """Write all demo project files to the store directory.

    Creates the same structure as a real project: project.md, state.md,
    stack.md, open-questions.md, 7 decisions (v2 format), and a snapshot.
    """
    store_path.mkdir(parents=True, exist_ok=True)
    decisions_dir = store_path / constants.DECISIONS_DIR
    decisions_dir.mkdir(exist_ok=True)
    snapshots_dir = store_path / constants.SNAPSHOTS_DIR
    snapshots_dir.mkdir(exist_ok=True)

    (store_path / constants.PROJECT_MD).write_text(PROJECT_MD)
    (store_path / constants.STATE_CURRENT_FILENAME).write_text(STATE_CURRENT_MD)
    (store_path / constants.STACK_MD).write_text(STACK_MD)
    (store_path / constants.OPEN_QUESTIONS_MD).write_text(OPEN_QUESTIONS_MD)

    # Emit decisions via format_decision_v2 so they match the writer's output shape.
    decision_files: dict[str, str] = {}
    for d in DEMO_DECISIONS:
        filename = _decision_filename(d, _DEMO_SLUGS[d.num])
        body = format_decision_v2(d)
        (decisions_dir / filename).write_text(body)
        decision_files[f"{constants.DECISIONS_DIR}/{filename}"] = body

    files = {
        constants.PROJECT_MD: PROJECT_MD,
        constants.STATE_CURRENT_FILENAME: STATE_CURRENT_MD,
        constants.STACK_MD: STACK_MD,
        constants.OPEN_QUESTIONS_MD: OPEN_QUESTIONS_MD,
        **decision_files,
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
