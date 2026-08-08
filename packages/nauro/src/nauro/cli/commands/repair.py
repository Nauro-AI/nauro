"""nauro repair — flip one supersede backref after the user confirms it.

`nauro doctor` detects and names; this command is the only place that acts on
what it names, and it acts on exactly one shape: a decision that records
`supersedes: N` where decision N is still active with no reciprocal
`superseded_by`. Everything else the planner sees comes back as guidance with
nothing written.

The gate is the point. A repair rewrites canonical decision history, so it
needs a human's word every time: the prompt names both decisions, states the
exact field change, and shows each side's version, date, and status so the user
can judge whether the newer decision really does retire the older one before
answering. There is deliberately no flag to skip it — a bypass would leave the
command's whole justification behind.

Ordering, and why: the plan is computed, shown, and confirmed outside any lock,
then recomputed and compared inside the decision write lock before the single
write. Confirming against a stale plan is the failure this closes. The journal
append happens only after the lock releases, keeping the store-wide invariant
that the journal lock never nests inside a resource lock.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from nauro_core.operations.repair import (
    RepairAssessment,
    RepairPlan,
    plan_supersede_repair,
)
from pydantic import BaseModel, ConfigDict

from nauro.cli.utils import cli_origin, resolve_target_project
from nauro.store.decision_lock import decision_write_lock
from nauro.store.filesystem_store import FilesystemStore
from nauro.store.journal import record_event
from nauro.store.post_commit import run_post_commit
from nauro.store.registry import (
    RegistryEntryV2,
    StoreBindingError,
    get_project_v2,
    validate_registry_entry_v2,
)

REPAIR_OPERATION = "repair_supersede_backref"


class StoreChangedDuringRepairError(RuntimeError):
    """The store moved between the confirmed plan and the locked write."""


class RepairEligibility(BaseModel):
    """Whether this project's store may be rewritten from this machine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eligible: bool
    guidance: str = ""


def classify_repair_eligibility(entry: RegistryEntryV2 | None) -> RepairEligibility:
    """Decide from the registry entry whether the local command may write.

    Both registered modes proceed today. A local project owns its store
    outright, and a cloud project's decisions are still plain markdown in the
    same local store, which `nauro sync` publishes afterwards, so neither has a
    store this command cannot legitimately rewrite.

    The refusal covers a project this machine cannot identify: repair rewrites
    canonical history, so it will not touch a store with no readable registry
    entry behind it. A hosted project whose store stops being locally
    authoritative attaches its refusal at the same point, routed to the hosted
    owner session rather than to a local write.
    """
    if entry is None:
        return RepairEligibility(
            eligible=False,
            guidance=(
                "This store has no readable registry entry on this machine, so "
                "repair cannot confirm which project owns it and will not "
                "rewrite it. Run 'nauro status'."
            ),
        )
    return RepairEligibility(eligible=True)


def _registry_entry(project_key: str) -> RegistryEntryV2 | None:
    """Return the validated registry entry for ``project_key``, or None."""
    raw = get_project_v2(project_key)
    if raw is None:
        return None
    try:
        return validate_registry_entry_v2(project_key, raw)
    except StoreBindingError:
        return None


def _label(num: int) -> str:
    return f"D{num}"


def _render_plan(plan: RepairPlan) -> list[str]:
    """Render the proposed flip and both sides' provenance for the prompt."""
    child = _label(plan.child_num)
    target = _label(plan.target_num)
    return [
        "Supersede backref orphan found:",
        "",
        f"  Child:  {child} (v{plan.child_version}, {plan.child_date}) {plan.child_title}",
        f"  Target: {target} (v{plan.target_version}, {plan.target_date}, "
        f"{plan.target_status.value}) {plan.target_title}",
        f"          {plan.target_path}",
        "",
        f"{child} records supersedes: {plan.target_num}, but {target} was never flipped.",
        "",
        f"This will set {target} status {plan.target_status.value} -> superseded, "
        f"superseded_by -> {child}.",
        "Nothing else in the file changes.",
        "",
    ]


def _render_assessment(assessment: RepairAssessment) -> list[str]:
    """Render everything the planner found, plan or not."""
    lines: list[str] = []

    # Rendered before the plan because a refusal on an unrelated pair does not
    # block an otherwise isolated orphan: the user sees both, then answers.
    if assessment.refusals:
        lines.append(f"Left for manual review ({len(assessment.refusals)}):")
        for refusal in assessment.refusals:
            lines.append(f"  {refusal.guidance}")
        lines.append("")

    if assessment.plan is not None:
        lines.extend(_render_plan(assessment.plan))
        return lines

    if len(assessment.orphans) > 1:
        lines.append(f"Supersede backref orphans ({len(assessment.orphans)}):")
        for orphan in assessment.orphans:
            lines.append(f"  {_label(orphan.child)} supersedes {_label(orphan.target)}")
        lines.append("")
        lines.append("Repair acts only on a single unambiguous orphan; resolve these by hand.")
        return lines

    if assessment.refusals:
        lines.append("Run 'nauro doctor' for the full store diagnosis.")
    else:
        lines.append("Nothing to repair: no supersede backref orphan found.")
    return lines


def _require_unchanged(planned: RepairPlan, current: RepairAssessment) -> None:
    """Abort unless the locked re-plan is the plan the user confirmed."""
    fresh = current.plan
    if (
        fresh is None
        or (fresh.child_num, fresh.target_num) != (planned.child_num, planned.target_num)
        or fresh.pre_image_digest != planned.pre_image_digest
    ):
        raise StoreChangedDuringRepairError(
            "The decision store changed after the repair was shown; nothing was "
            "written. Re-run 'nauro repair'."
        )


def _journal_repair(store_path: Path, plan: RepairPlan) -> None:
    """Record the committed flip. Fail-open: never fails the repair."""
    record_event(
        store_path,
        operation=REPAIR_OPERATION,
        target=json.dumps(
            {"child": plan.child_num, "target": plan.target_num},
            sort_keys=True,
            separators=(",", ":"),
        ),
        status="committed",
        payload={
            "child": plan.child_num,
            "target": plan.target_num,
            "target_stem": plan.target_stem,
            "pre_image_digest": plan.pre_image_digest,
        },
        origin_factory=cli_origin,
        decision_id=plan.target_stem,
    )


def repair(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Repair a supersede backref orphan after confirming it.

    Offers the single unambiguous case: one decision records 'supersedes: N'
    while decision N is still active with no 'superseded_by' pointing back.
    Every other shape is reported with guidance and left alone. Run
    'nauro doctor' first to see the full store diagnosis.
    """
    project_name, store_path = resolve_target_project(project)
    typer.echo(f"Project: {project_name}\n")

    eligibility = classify_repair_eligibility(_registry_entry(store_path.name))
    if not eligibility.eligible:
        typer.echo(eligibility.guidance)
        return

    assessment = plan_supersede_repair(FilesystemStore(store_path))
    for line in _render_assessment(assessment):
        typer.echo(line)

    plan = assessment.plan
    if plan is None:
        return

    if not typer.confirm("Apply this repair?", default=False):
        typer.echo("No changes written.")
        return

    try:
        # One lock, taken once: decision_write_lock already holds
        # decisions/.lock, and flock is not reentrant across descriptors, so a
        # second lock on the same resource inside this block would deadlock.
        with decision_write_lock(store_path):
            _require_unchanged(plan, plan_supersede_repair(FilesystemStore(store_path)))
            FilesystemStore(store_path).write_file(plan.target_path, plan.new_content)
    except StoreChangedDuringRepairError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _journal_repair(store_path, plan)

    typer.echo(f"Repaired {_label(plan.target_num)}: now superseded by {_label(plan.child_num)}.")
    typer.echo(f"  {store_path / plan.target_path}")

    outcome = run_post_commit(
        store_path,
        snapshot_trigger=(
            f"repair: {_label(plan.target_num)} superseded by {_label(plan.child_num)}"
        ),
        regenerate_agents_md=True,
        warn=lambda msg: typer.echo(msg, err=True),
    )
    for line in outcome.warnings:
        typer.echo(line, err=True)
    for repo_path in outcome.updated_repos:
        typer.echo(f"  Updated AGENTS.md: {repo_path}")
    typer.echo("Run 'nauro sync' to publish this change.")
