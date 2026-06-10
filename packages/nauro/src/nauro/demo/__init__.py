"""Demo project templates for `nauro init --demo`.

Creates a sample project with pre-written decisions, state, questions,
and a snapshot — so users can explore Nauro's features immediately.

Decision files are built as ``Decision`` objects and serialized via
``format_decision`` so the demo output is guaranteed to match whatever
real writer output looks like (no template drift).
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from nauro_core.decision_model import (
    Decision,
    DecisionConfidence,
    DecisionSource,
    DecisionStatus,
    DecisionType,
    RejectedAlternative,
    Reversibility,
    format_decision,
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
    *,
    decided: date = _DEMO_DATE,
    status: DecisionStatus = DecisionStatus.active,
    supersedes: str | None = None,
    superseded_by: str | None = None,
) -> Decision:
    return Decision(
        date=decided,
        version=1,
        status=status,
        confidence=confidence,
        decision_type=decision_type,
        reversibility=reversibility,
        source=DecisionSource.manual,
        num=num,
        title=title,
        rationale=rationale,
        rejected=rejected,
        supersedes=supersedes,
        superseded_by=superseded_by,
    )


# ── Demo decisions ──
#
# The first seven carry the original demo prose at the shared 2026-03-15 date.
# Decisions 8 through 13 add the two supersession structures the demo store was
# always meant to show, dated across the project's timeline so the graph's
# timeline and lineage views both read:
#
#   Consolidation fan: decision 13 (a unified Express middleware stack) retires
#   three earlier ad-hoc cross-cutting approaches (8, 9, 10). It carries a scalar
#   supersedes pointing at the earliest of the three (8); all three flip to
#   superseded and carry superseded_by pointing back at 13. This mirrors the
#   on-disk shape that propose_decision's supersede path writes.
#
#   Short chain: decision 11 (in-memory per-process rate limiting) is superseded
#   by decision 12 (rate limiting at the API gateway), a two-step lineage.

DEMO_DECISIONS: list[Decision] = [
    _decision(
        1,
        "PostgreSQL over MongoDB",
        DecisionConfidence.high,
        DecisionType.data_model,
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
        "REST over GraphQL",
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
        "Monorepo over polyrepo",
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
        "SSE over WebSocket for live updates",
        DecisionConfidence.high,
        DecisionType.infrastructure,
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
        "No background workers",
        DecisionConfidence.medium,
        DecisionType.pattern,
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
        "Cursor-based pagination",
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
        "Hard delete with audit log",
        DecisionConfidence.high,
        DecisionType.data_model,
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
    # ── Consolidation fan: 8, 9, 10 are early ad-hoc cross-cutting approaches,
    #    all retired by 13 once the pattern was clear. ──
    _decision(
        8,
        "Inline request validation",
        DecisionConfidence.low,
        DecisionType.pattern,
        Reversibility.easy,
        "Each route handler validates its own request body inline at the top of "
        "the function, checking required fields and types by hand before touching "
        "the database. This kept the first handlers small and avoided pulling in a "
        "validation library while the request shapes were still in flux.",
        [
            RejectedAlternative(
                name="A shared validation schema layer",
                reason=(
                    "Felt premature with only a handful of endpoints and request shapes "
                    "still changing weekly. Hand-written checks were faster to write for "
                    "the first three handlers than agreeing on a schema format."
                ),
            ),
        ],
        decided=date(2026, 1, 22),
        status=DecisionStatus.superseded,
        superseded_by="13",
    ),
    _decision(
        9,
        "Endpoint error mapping",
        DecisionConfidence.low,
        DecisionType.pattern,
        Reversibility.easy,
        "Each handler wraps its body in a try/catch and maps caught errors to HTTP "
        "status codes locally, so the endpoint that knows the failure decides the "
        "response. Early on this made each handler self-contained and easy to read "
        "in isolation.",
        [
            RejectedAlternative(
                name="A central error-handling middleware",
                reason=(
                    "Not worth the indirection while there were only two error shapes. "
                    "Local try/catch kept the mapping next to the code that raised it, "
                    "which read clearly for the first endpoints."
                ),
            ),
        ],
        decided=date(2026, 2, 5),
        status=DecisionStatus.superseded,
        superseded_by="13",
    ),
    _decision(
        10,
        "Logging calls in each handler",
        DecisionConfidence.low,
        DecisionType.pattern,
        Reversibility.easy,
        "Handlers call the structured logger directly, emitting a start and end log "
        "line with the request id and outcome. Putting the calls in the handler let "
        "each one log exactly the fields that mattered for its own path without a "
        "shared convention to agree on first.",
        [
            RejectedAlternative(
                name="A logging middleware around every request",
                reason=(
                    "A uniform middleware log line would have missed the per-handler "
                    "fields we cared about, and we had not yet settled which fields those "
                    "were. Hand-placed calls were the fastest way to learn what to log."
                ),
            ),
        ],
        decided=date(2026, 2, 18),
        status=DecisionStatus.superseded,
        superseded_by="13",
    ),
    # ── Short chain: 11 superseded by 12. ──
    _decision(
        11,
        "In-process rate limiting",
        DecisionConfidence.medium,
        DecisionType.infrastructure,
        Reversibility.moderate,
        "Rate limiting is enforced in process with an in-memory counter per client, "
        "reset on a sliding window. With a single container this is exact, adds no "
        "network hop, and needed no extra infrastructure to ship the first abuse "
        "protection.",
        [
            RejectedAlternative(
                name="A shared counter in Redis",
                reason=(
                    "Added a dependency and a network round-trip per request before we "
                    "had evidence we needed cross-instance limits. A single container "
                    "made the in-memory counter correct for the launch traffic."
                ),
            ),
        ],
        decided=date(2026, 2, 26),
        status=DecisionStatus.superseded,
        superseded_by="12",
    ),
    _decision(
        12,
        "Rate limiting at the API gateway",
        DecisionConfidence.high,
        DecisionType.infrastructure,
        Reversibility.moderate,
        "Rate limiting moved to the API gateway, which enforces a shared per-client "
        "budget across every container before a request reaches application code. "
        "Once the service scaled past one instance, per-process counters each saw "
        "only a fraction of a client's traffic, so a client could exceed the "
        "intended limit by a factor of the container count. Enforcing at the gateway "
        "restores one consistent budget and keeps the limiting load off the app.",
        [
            RejectedAlternative(
                name="A shared counter in Redis",
                reason=(
                    "Would make the limit consistent across instances but keeps every "
                    "request paying a round-trip and puts the application on the critical "
                    "path for abuse protection. The gateway already sees all traffic and "
                    "rejects over-budget requests before they cost the app anything."
                ),
            ),
        ],
        decided=date(2026, 4, 20),
        supersedes="11",
    ),
    # ── Consolidation: 13 retires the three ad-hoc approaches (8, 9, 10). ──
    _decision(
        13,
        "Unified Express middleware stack for validation, errors, and logging",
        DecisionConfidence.high,
        DecisionType.architecture,
        Reversibility.moderate,
        "Request validation, error mapping, and request logging move into one "
        "ordered Express middleware stack applied to every route. Handlers receive "
        "an already-validated body, throw typed errors that a single error "
        "middleware maps to responses, and emit no logging of their own. The three "
        "concerns had been handled inline in each handler, which drifted: validation "
        "rules diverged between endpoints, two handlers mapped the same error to "
        "different status codes, and several paths logged nothing on failure. One "
        "stack makes the behavior uniform and keeps handlers focused on their own "
        "logic.",
        [
            RejectedAlternative(
                name="Keeping the concerns inline but extracting shared helpers",
                reason=(
                    "Shared helpers still leave each handler responsible for calling them "
                    "in the right order, which is exactly what drifted. A middleware stack "
                    "applies the order once and cannot be forgotten by a new handler."
                ),
            ),
            RejectedAlternative(
                name="A heavier framework with the cross-cutting concerns built in",
                reason=(
                    "Rewriting onto a new framework to get middleware conventions would "
                    "touch every handler for a gain Express middleware already delivers "
                    "without the migration."
                ),
            ),
        ],
        decided=date(2026, 5, 18),
        supersedes="8",
    ),
]


DEMO_STATE_CURRENT_MD = f"""\
# Current State

Implementing user authentication \u2014 building JWT-based auth flow \
with refresh tokens and RBAC.

*Last updated: {_DEMO_DATE.isoformat()}T12:00Z*
"""

OPEN_QUESTIONS_MD = """\
# Open Questions
- [Q1] Should we add rate limiting at the API gateway or application layer?
- [Q2] Redis vs in-memory caching for session storage?
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
    8: "inline-request-validation",
    9: "per-endpoint-error-mapping",
    10: "logging-calls-in-handlers",
    11: "in-memory-rate-limiting",
    12: "rate-limiting-at-gateway",
    13: "middleware-stack-consolidation",
}


def create_demo_project(store_path: Path) -> None:
    """Write all demo project files to the store directory.

    Creates the same structure as a real project: project.md, state.md,
    stack.md, open-questions.md, the demo decisions (v2 format) including the
    supersession structures, and a snapshot.
    """
    store_path.mkdir(parents=True, exist_ok=True)
    decisions_dir = store_path / constants.DECISIONS_DIR
    decisions_dir.mkdir(exist_ok=True)
    snapshots_dir = store_path / constants.SNAPSHOTS_DIR
    snapshots_dir.mkdir(exist_ok=True)

    (store_path / constants.PROJECT_MD).write_text(PROJECT_MD, encoding="utf-8")
    (store_path / constants.STATE_CURRENT_FILENAME).write_text(
        DEMO_STATE_CURRENT_MD, encoding="utf-8"
    )
    (store_path / constants.STACK_MD).write_text(STACK_MD, encoding="utf-8")
    (store_path / constants.OPEN_QUESTIONS_MD).write_text(OPEN_QUESTIONS_MD, encoding="utf-8")

    # Emit decisions via format_decision so they match the writer's output shape.
    decision_files: dict[str, str] = {}
    for d in DEMO_DECISIONS:
        filename = _decision_filename(d, _DEMO_SLUGS[d.num])
        body = format_decision(d)
        (decisions_dir / filename).write_text(body, encoding="utf-8")
        decision_files[f"{constants.DECISIONS_DIR}/{filename}"] = body

    files = {
        constants.PROJECT_MD: PROJECT_MD,
        constants.STATE_CURRENT_FILENAME: DEMO_STATE_CURRENT_MD,
        constants.STACK_MD: STACK_MD,
        constants.OPEN_QUESTIONS_MD: OPEN_QUESTIONS_MD,
        **decision_files,
    }

    snapshot = {
        "schema_version": 1,
        "version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": "demo project created",
        "trigger_detail": "",
        "token_count": sum(len(v) for v in files.values()) // 4,
        "files": files,
    }

    (snapshots_dir / "v001.json").write_text(
        json.dumps(snapshot, indent=2) + "\n", encoding="utf-8"
    )
