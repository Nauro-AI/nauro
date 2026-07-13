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
# The first seven carry the product's foundational choices at the shared
# 2026-03-15 date. Decisions 8 through 13 add two supersession structures,
# dated across the project's timeline so the graph's timeline and lineage
# views both read:
#
#   Consolidation fan: decision 13 (one ordered transaction pipeline) retires
#   three earlier per-screen approaches (8 inline category guessing, 9 per-screen
#   amount formatting, 10 dashboard-only de-duplication). It carries a scalar
#   supersedes pointing at the earliest of the three (8); all three flip to
#   superseded and carry superseded_by pointing back at 13. This mirrors the
#   on-disk shape that propose_decision's supersede path writes.
#
#   Short chain: decision 11 (calendar-month budget periods) is superseded by
#   decision 12 (pay-cycle budget periods), a two-step lineage.

DEMO_DECISIONS: list[Decision] = [
    _decision(
        1,
        "Amounts stored in integer cents, never floating point",
        DecisionConfidence.high,
        DecisionType.data_model,
        Reversibility.hard,
        "Every monetary amount (transactions, budgets, balances) is stored as an integer "
        "number of cents and formatted to dollars only for display. Binary floating point "
        "cannot represent most decimal amounts exactly (0.10 + 0.20 comes out as "
        "0.30000000000000004), so with floating-point dollars, sums and budgets drift by a "
        "cent and a balance that should read 0.00 shows -0.01. Integer cents are exact, "
        "every calculation is plain integer math, and decimals appear only at the "
        "formatting boundary when an amount is shown to the user.",
        [
            RejectedAlternative(
                name="Floating-point dollars (a decimal amount field)",
                reason=(
                    "The obvious representation, and what a quick prototype reaches for, but "
                    "binary floating point cannot hold values like 0.10 exactly, so totals "
                    "accumulate rounding error and reconciliations fail by a penny. A "
                    "fixed-precision decimal type avoids the drift but is heavier than integer "
                    "math and still lets a stray float coercion reintroduce the error. Integer "
                    "cents keep every amount exact with plain integers."
                ),
            ),
        ],
    ),
    _decision(
        2,
        "One-time purchase, no subscription",
        DecisionConfidence.medium,
        DecisionType.pattern,
        Reversibility.moderate,
        "Pennykeep is a single upfront purchase; there is no recurring fee. Budgeting "
        "is the one category where a monthly charge is self-defeating: asking someone "
        "who is trying to control their spending to accept yet another subscription is "
        "exactly the friction that sent them looking for a budgeting tool. A one-time "
        "price also fits the local-first model: with no server to run per user, there "
        "is no per-user cost to recover every month.",
        [
            RejectedAlternative(
                name="Monthly subscription",
                reason=(
                    "Recurring revenue is smoother for the business, but in this category "
                    "it reads as precisely the kind of small recurring charge the user is "
                    "trying to hunt down and cancel. Early testers resented paying every "
                    "month for a tool whose whole job is to question recurring charges, and "
                    "the churn showed it."
                ),
            ),
        ],
    ),
    _decision(
        3,
        "On-device storage, no cloud account",
        DecisionConfidence.high,
        DecisionType.data_model,
        Reversibility.hard,
        "Budgets and transactions live in an encrypted store on the device. There "
        "is no server and no account. A budgeting app that keeps people's financial "
        "history on a server is a standing breach liability and an ongoing "
        "operational cost, and it asks every user to trust an operator they cannot "
        "audit. Local-first keeps the data private and available offline, and leaves "
        "nothing central to secure or subpoena. The catch worth stating plainly: "
        "this is a product boundary, not a missing feature. There is no server to "
        "bolt on later without re-deciding the whole privacy model.",
        [
            RejectedAlternative(
                name="Cloud sync across devices (server accounts)",
                reason=(
                    "Holding budgets and transactions on a server so they sync across a "
                    "user's devices is the obvious convenience ask, but it makes the app a "
                    "custodian of financial data: a breach liability, a compliance surface, "
                    "and a running cost, all for a single-user tool. Local-first gives up "
                    "multi-device convenience to keep the promise that the data never "
                    "leaves the owner's hands."
                ),
            ),
        ],
    ),
    _decision(
        4,
        "Native mobile app over web app",
        DecisionConfidence.medium,
        DecisionType.architecture,
        Reversibility.hard,
        "Pennykeep ships as native iOS and Android apps rather than a web or PWA front "
        "end. A single web codebase is cheaper to maintain, but the three things this "
        "product leans on are all first-class on native and second-class in a browser: "
        "a fully offline session, an encrypted on-phone store, and a biometric lock on "
        "open. Building those to a standard people would trust with their finances "
        "fights the browser the whole way; on native they are platform features.",
        [
            RejectedAlternative(
                name="Web app / PWA",
                reason=(
                    "One codebase across platforms is the obvious economy, but "
                    "offline-by-default storage, OS-backed encryption at rest, and Face ID / "
                    "fingerprint unlock are exactly where a PWA degrades to a best-effort "
                    "imitation. For an app whose selling point is that your financial data "
                    "stays locked on your phone, best-effort is not enough."
                ),
            ),
        ],
    ),
    _decision(
        5,
        "Passcode and biometric lock, no user accounts",
        DecisionConfidence.high,
        DecisionType.data_model,
        Reversibility.hard,
        "The app is protected by the device passcode and Face ID / fingerprint, not by "
        "a username and password. There is no server to authenticate against, so a "
        "login would be theatre: it would introduce an attack surface and a credential "
        "to leak while protecting nothing that is not already behind the phone's own "
        "lock screen. Tying access to the device's biometric gate keeps the security "
        "model honest with the local-first storage decision.",
        [
            RejectedAlternative(
                name="Email and password login",
                reason=(
                    "A familiar login screen looks reassuring, but with no server behind it "
                    "there is nothing to sign in to: the data is already on the phone. It "
                    "would only add a password for the user to reuse and for us to be blamed "
                    "for leaking, with no security gain over the OS lock."
                ),
            ),
        ],
    ),
    _decision(
        6,
        "Envelope budgeting method",
        DecisionConfidence.medium,
        DecisionType.pattern,
        Reversibility.moderate,
        "The core model is envelope budgeting: every dollar of income is assigned to a "
        "named category envelope before it is spent, and spending draws down a specific "
        "envelope. The alternative, showing people where their money went after the "
        "fact, is easier to build and easier to sell, but a rear-view report does not "
        "change behavior. Committing each dollar in advance is what turns a tracker "
        "into a budget.",
        [
            RejectedAlternative(
                name="Passive spending tracker",
                reason=(
                    "Categorizing past transactions into charts is lower-friction and demos "
                    "well, but it answers 'where did it go?' rather than 'what is this for?'. "
                    "Testers who only saw past spend kept overspending; the ones who had to "
                    "assign money up front changed what they did."
                ),
            ),
        ],
    ),
    _decision(
        7,
        "No ads, no data monetization",
        DecisionConfidence.high,
        DecisionType.pattern,
        Reversibility.moderate,
        "The only revenue is the one-time purchase. The app shows no ads and sells no "
        "data, anonymized or otherwise. A budgeting app funded by monetizing financial "
        "data is a contradiction in terms, and the local-first architecture makes it "
        "not merely wrong but impossible: the data never reaches us, so there is "
        "nothing to package or resell. Writing it down as a decision keeps a future "
        "growth push from quietly proposing 'anonymized insights' as a revenue line.",
        [
            RejectedAlternative(
                name="Free with ads / anonymized data resale",
                reason=(
                    "Ad-supported and data-resale models lower the price of entry, but for a "
                    "tool holding someone's spending history they poison the trust the "
                    "product is built on. And under local-first there is no user data on our "
                    "side to aggregate or sell, so the model could not be built even if we "
                    "wanted it."
                ),
            ),
        ],
    ),
    # ── Consolidation fan: 8, 9, 10 are early per-screen approaches to
    #    categorization, formatting, and de-duplication, all retired by 13 once
    #    the transaction pipeline was the clear shape. ──
    _decision(
        8,
        "Inline category guessing in the import screen",
        DecisionConfidence.low,
        DecisionType.pattern,
        Reversibility.easy,
        "The CSV import screen guesses each transaction's category inline, matching the "
        "merchant string against a hardcoded list of keywords right where the rows are "
        "parsed. With only a handful of recognized merchants this kept the first import "
        "flow self-contained and avoided standing up a shared rules layer before we "
        "knew what the rules should be.",
        [
            RejectedAlternative(
                name="A shared categorization rules layer",
                reason=(
                    "A general rules engine felt premature for the dozen merchants we "
                    "recognized at first. Inline keyword checks were faster to write and "
                    "easy to read next to the parsing code for the first screen that needed "
                    "them."
                ),
            ),
        ],
        decided=date(2026, 1, 22),
        status=DecisionStatus.superseded,
        superseded_by="13",
    ),
    _decision(
        9,
        "Per-screen amount formatting",
        DecisionConfidence.low,
        DecisionType.pattern,
        Reversibility.easy,
        "Each screen formats currency amounts itself, calling the platform number "
        "formatter with its own sign and rounding conventions at the point of display. "
        "With only two screens showing money early on, a shared formatter was more "
        "indirection than the duplication it would have removed.",
        [
            RejectedAlternative(
                name="A shared money formatter",
                reason=(
                    "Extracting one formatting helper is the textbook move, but for two call "
                    "sites it was not worth the indirection, and each screen wanted slightly "
                    "different sign and rounding treatment while the designs were still "
                    "moving."
                ),
            ),
        ],
        decided=date(2026, 2, 5),
        status=DecisionStatus.superseded,
        superseded_by="13",
    ),
    _decision(
        10,
        "Duplicate detection in the dashboard",
        DecisionConfidence.low,
        DecisionType.pattern,
        Reversibility.easy,
        "The dashboard de-duplicates re-imported rows itself, comparing new "
        "transactions against what it already shows by date, amount, and merchant "
        "before rendering. Only the dashboard re-imported at first, so putting the "
        "check there kept it close to the screen that needed it rather than a shared "
        "step every importer would have to call.",
        [
            RejectedAlternative(
                name="A shared de-duplication step",
                reason=(
                    "A common de-dup pass sounded right in principle, but only the dashboard "
                    "was re-importing early on, so a shared step would have been "
                    "infrastructure for a single caller. Doing it inline was the smaller move "
                    "while that stayed true."
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
        "Calendar-month budget periods",
        DecisionConfidence.medium,
        DecisionType.pattern,
        Reversibility.moderate,
        "Budgets run on the calendar month and reset on the 1st. It is the obvious "
        "default (everyone knows what 'this month' means), and it was less code than "
        "modeling arbitrary period boundaries for the first version of the budgeting "
        "core.",
        [
            RejectedAlternative(
                name="Pay-cycle periods",
                reason=(
                    "Aligning periods to a user's payday would model how money actually "
                    "arrives, but it meant carrying a configurable cycle and handling partial "
                    "first periods. Calendar months were the obvious, cheaper default to ship "
                    "the first budget loop."
                ),
            ),
        ],
        decided=date(2026, 2, 26),
        status=DecisionStatus.superseded,
        superseded_by="12",
    ),
    _decision(
        12,
        "Pay-cycle budget periods",
        DecisionConfidence.high,
        DecisionType.pattern,
        Reversibility.moderate,
        "Budget periods now align to the user's payday rather than the calendar 1st. "
        "Money arrives on payday, and that is when people mentally reset their budget; "
        "a calendar month looked flush on the 30th and then broke on the 2nd, when rent "
        "cleared before the 'new month' of income had landed. Anchoring the period to "
        "payday makes the numbers on screen match how the user actually experiences "
        "their month.",
        [
            RejectedAlternative(
                name="Keep calendar months / arbitrary start day",
                reason=(
                    "The calendar 1st is simplest and a user-chosen start day is the flexible "
                    "option, but neither matches the one date that actually governs a "
                    "person's cash: payday. Testers on a fixed calendar month kept seeing a "
                    "healthy balance that was really next month's rent still sitting in the "
                    "account."
                ),
            ),
        ],
        decided=date(2026, 4, 20),
        supersedes="11",
    ),
    # ── Consolidation: 13 retires the three per-screen approaches (8, 9, 10). ──
    _decision(
        13,
        "Unified transaction pipeline for categorization, formatting, and de-duplication",
        DecisionConfidence.high,
        DecisionType.architecture,
        Reversibility.moderate,
        "Every imported transaction now passes through one ordered pipeline "
        "(de-duplicate, categorize, then format) before any screen sees it. The three "
        "concerns had been handled inline on whichever screen first needed them, and "
        "they drifted: the import screen and a later quick-add path guessed categories "
        "from different keyword lists, two screens formatted the same amount "
        "differently, and duplicate rows slipped through everywhere except the "
        "dashboard. One pipeline applied at the point of import makes every screen "
        "consume already-clean, already-categorized, already-formatted transactions.",
        [
            RejectedAlternative(
                name="Keep the logic inline but extract shared helpers",
                reason=(
                    "Pulling the keyword matcher, the formatter, and the de-dup check into "
                    "shared helpers removes the copy-paste, but each screen still decides "
                    "whether and in what order to call them, which is exactly what drifted. "
                    "A single pipeline applies the order once, and a new screen cannot forget "
                    "a step."
                ),
            ),
            RejectedAlternative(
                name="Adopt a heavier data-layer library that bundles these concerns",
                reason=(
                    "A batteries-included import library would provide categorization and "
                    "de-dup out of the box, but adopting it is a rewrite of the transaction "
                    "path to gain what one small ordered pipeline already delivers, and it "
                    "pulls a large dependency into an app that ships almost nothing else."
                ),
            ),
        ],
        decided=date(2026, 5, 18),
        supersedes="8",
    ),
]


DEMO_STATE_CURRENT_MD = f"""\
# Current State

Building the CSV import flow: mapping statement columns to the transaction \
model and running each row through the categorization pipeline before it reaches \
the dashboard.

*Last updated: {_DEMO_DATE.isoformat()}T12:00Z*
"""

OPEN_QUESTIONS_MD = """\
# Open Questions
- [Q1] Should an unspent envelope roll over to the next period, or reset each period?
- [Q2] Add an optional encrypted export for manual transfer, or stay strictly single-device?
"""

PROJECT_MD = """\
# Pennykeep

**One-liner:** A local-first budgeting app that keeps your money on your phone, not on a server.

## Goals
- Ship a native budgeting app where every transaction and budget lives encrypted on the phone
- Help everyday people run an envelope budget without linking a bank or creating an account

## Non-goals
- No cloud account and no third-party bank linking; nothing is uploaded to a server or aggregator
- Not a wealth-management or investing tool; this is day-to-day budgeting

## Users
Everyday people who want to budget without linking their bank to a third-party app.
They are comfortable typing or importing transactions, and they care more about
privacy and staying offline than about automatic multi-account aggregation.

## Constraints
- Must ship the first iOS build by June 2026
- All financial data stays in an encrypted on-phone store (SQLCipher); no server holds it
- Single-developer maintenance budget: keep the operational surface near zero
"""

STACK_MD = """\
# Stack

## Language & Framework
- **React Native + Expo**. Chosen for: one codebase across iOS and Android with \
native modules for biometric unlock and encrypted storage. Rejected: Flutter \
(capable, but the team's TypeScript experience carries over to React Native), \
two separate native codebases (best platform fit, but double the build for a small team).

## Storage
- **SQLCipher-encrypted SQLite**. Chosen for: a single-file encrypted database \
that lives on the phone and never leaves it, matching the local-first promise. \
Rejected: plain SQLite (no encryption at rest for financial data), a hosted \
database (would put transactions on a server, which the product explicitly refuses).

## Budgeting Core
- **Envelope allocation engine**. Chosen for: every dollar is assigned to a \
category envelope, the model the product is built around. Rejected: a passive \
spend tracker (reports past spending but does not change behavior), spreadsheet \
import only (flexible, but leaves the budgeting logic to the user).

## Key Libraries
- **expo-local-authentication** for the Face ID / fingerprint lock on open
- **A CSV parser** for importing bank statement exports into the transaction pipeline
"""


def _decision_filename(decision: Decision, slug: str) -> str:
    return f"{decision.num:03d}-{slug}.md"


_DEMO_SLUGS = {
    1: "amounts-in-integer-cents",
    2: "one-time-purchase-no-subscription",
    3: "on-device-storage-no-cloud-account",
    4: "native-mobile-over-web-app",
    5: "passcode-biometric-lock-no-accounts",
    6: "envelope-budgeting-method",
    7: "no-ads-no-data-monetization",
    8: "inline-category-guessing-import",
    9: "per-screen-amount-formatting",
    10: "duplicate-detection-in-dashboard",
    11: "calendar-month-budget-periods",
    12: "pay-cycle-budget-periods",
    13: "unified-transaction-pipeline",
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
