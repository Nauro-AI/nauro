"""Guided existing-hosted conversion, pending connection-flow rollout."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.generation_migration_assessment import assess_legacy_migration
from nauro.store.generation_migration_plan import prepare_legacy_migration_plan
from nauro.store.migration_admission import (
    MigrationAdmission,
    MigrationAdmissionError,
    inspect_migration,
)
from nauro.sync.generation_acquisition import acquire_generation_projection
from nauro.sync.generation_attachment import InitialAttachmentSession
from nauro.sync.migration_admission import (
    decide_migration_assessment,
    load_migration_plan,
    save_migration_assessment,
    verify_migration_source,
)
from nauro.sync.migration_installation import (
    continue_migration_installation,
    verify_admitted_migration_source,
)
from nauro.sync.migration_preservation import (
    decode_migration_plan,
    require_registered_migration_source,
)
from nauro.sync.remote import TransferBoundaryError


def _prepare(
    session: InitialAttachmentSession, prior: MigrationAdmission | None
) -> MigrationAdmission:
    binding = session.binding
    session.require_binding(binding)
    projection = acquire_generation_projection(
        binding, active_user_id=session.actor, session=session
    )
    assessment = assess_legacy_migration(projection)
    plan = prepare_legacy_migration_plan(assessment)
    session.require_binding(binding)
    return save_migration_assessment(plan, replace=prior)


def _present(record: MigrationAdmission, emit: Callable[[str], None]) -> None:
    current, raw = load_migration_plan(Path(record.store))
    if current != record:
        raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
    plan = decode_migration_plan(raw, record)
    emit("This hosted project already uses server-authorized generations.")
    emit(
        "This upgrade changes this computer's working copy. Deferring does not "
        "undo the server transition."
    )
    emit(f"Saved upgrade: {record.migration_id}. Status: {record.phase}.")
    differences = [entry for entry in plan.entries if entry.disposition == "quarantine"]
    unsupported = "unsupported, preserved outside the active record with export"
    if differences:
        emit("These files differ from the hosted record or exist only on this computer:")
        for entry in differences:
            label = "local-only" if entry.source_class == "local_only" else "different from hosted"
            if entry.recovery_kind == "preserved_unsupported":
                label += "; " + unsupported
            emit(f"  {json.dumps(entry.source_path, ensure_ascii=False)}: {label}.")
        emit(
            "Continuing preserves these files outside the active record. They "
            "will not appear in ordinary project reads."
        )
        emit(
            "Nothing is merged or submitted. Any later new or revised decision "
            "requires its normal approval."
        )
    evidence = [entry for entry in plan.entries if entry.source_class == "evidence"]
    if evidence:
        emit("Other local files retained outside the active record:")
        for entry in evidence:
            note = (
                ": " + unsupported + "." if entry.recovery_kind == "preserved_unsupported" else ""
            )
            emit(f"  {json.dumps(entry.source_path, ensure_ascii=False)}{note}")
    emit(
        "Preservation folder, beside the project store: "
        f"{json.dumps(plan.backup_directory_name, ensure_ascii=False)}."
    )
    emit(
        "Defer if these files must remain in this computer's active record. "
        "Preserved files remain available for export."
    )
    snapshots = sum(entry.source_class == "snapshot" for entry in plan.entries)
    emit(
        f"All {len(plan.entries)} source files, including {snapshots} local "
        f"snapshots, will remain preserved."
    )
    emit("Local snapshots are retained for recovery, not imported into hosted history.")
    emit(
        "Unchanged approved decisions do not require approval again. No files "
        "are treated as deletion instructions."
    )


class _AssessmentChangedError(MigrationAdmissionError):
    pass


def _execute(
    record: MigrationAdmission, session: InitialAttachmentSession
) -> tuple[MigrationAdmission, bytes]:
    binding = session.binding
    session.require_binding(binding)
    if record.phase == "assessed":
        require_registered_migration_source(record)
    if record.phase in {"replacing", "installing"}:
        current, raw = load_migration_plan(binding.store_path)
        if current != record:
            raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
        try:
            verify_admitted_migration_source(record, raw)
        except MigrationAdmissionError as exc:
            raise _AssessmentChangedError(str(exc)) from exc
    projection = acquire_generation_projection(
        binding, active_user_id=record.actor, session=session
    )
    current, raw = load_migration_plan(binding.store_path)
    if current != record:
        raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
    if decode_migration_plan(raw, record).projection != projection.target.identity:
        raise _AssessmentChangedError(
            "The hosted generation changed. A fresh assessment and confirmation "
            "are required before conversion."
        )
    if record.phase in {"assessed", "blocked"}:
        try:
            verify_migration_source(record, raw)
        except MigrationAdmissionError as exc:
            raise _AssessmentChangedError(str(exc)) from exc
    if record.phase == "assessed":
        record = decide_migration_assessment(record, preserve=True)
    return continue_migration_installation(record, projection, session), raw


def guided_existing_hosted_upgrade(
    session: InitialAttachmentSession,
    *,
    emit: Callable[[str], None],
    confirm: Callable[[str], bool],
) -> MigrationAdmission | None:
    """Prepare, explain and explicitly continue one saved local upgrade."""
    if not isinstance(session, InitialAttachmentSession):
        raise MigrationAdmissionError("The upgrade requires the captured owner connection.")
    binding = session.binding
    record = inspect_migration(binding.store_path)
    if record is not None:
        current, _ = load_migration_plan(binding.store_path)
        if current != record:
            raise MigrationAdmissionError("The saved upgrade changed; reopen connection setup.")
        if (record.project_id, record.endpoint, record.actor) != (
            binding.project_id,
            binding.server_url,
            session.actor,
        ):
            raise MigrationAdmissionError("The saved upgrade belongs to another connection.")
    if record is not None and record.phase == "completed":
        emit(
            "This computer has a completed upgrade record. Use the normal read "
            "or sync path to verify current access."
        )
        return record
    if record is not None and record.phase == "declined":
        if not confirm("The earlier upgrade was deferred. Prepare a fresh assessment?"):
            emit("Upgrade remains deferred. No source files changed.")
            return record
        record = _prepare(session, record)
    elif record is None:
        record = _prepare(session, None)
    return _guide(record, session, emit, confirm)


def _guide(
    record: MigrationAdmission,
    session: InitialAttachmentSession,
    emit: Callable[[str], None],
    confirm: Callable[[str], bool],
) -> MigrationAdmission | None:
    binding = session.binding
    for attempt in range(2):
        _present(record, emit)
        if record.phase == "assessed":
            consent = confirm(
                "Preserve the listed local files outside the active record and "
                "upgrade this computer?"
            )
            if not consent:
                declined = decide_migration_assessment(record, preserve=False)
                emit("Upgrade deferred. The local working copy remains unchanged.")
                return declined
        elif not confirm(
            "Continue this saved upgrade using its existing identity and "
            "approved file dispositions?"
        ):
            emit(
                "Upgrade remains incomplete. Preserved evidence and the local "
                "access block remain in place."
            )
            return record
        try:
            completed, raw = _execute(record, session)
        except (
            OSError,
            MigrationAdmissionError,
            GenerationAuthorityError,
            TransferBoundaryError,
            httpx.HTTPError,
        ) as exc:
            current = inspect_migration(binding.store_path)
            if isinstance(exc, _AssessmentChangedError) and record.phase != "assessed":
                emit(
                    "Upgrade incomplete: the hosted record or project files changed "
                    "after this upgrade was admitted."
                )
                emit(
                    "This saved upgrade cannot continue against the changed record. "
                    "Local writes remain blocked and preserved evidence is retained. "
                    "Owner recovery is required."
                )
                return current
            emit(f"Upgrade incomplete: {exc}")
            if (
                isinstance(exc, _AssessmentChangedError)
                and current == record
                and record.phase == "assessed"
                and attempt == 0
                and confirm(
                    "Review a fresh assessment of the changed record? This does "
                    "not approve conversion."
                )
            ):
                record = _prepare(session, record)
                continue
            emit(
                "Retained evidence was not discarded. Reopen the same connection "
                "flow to inspect this saved upgrade."
            )
            return current
        emit(
            "This computer now reads the verified hosted record. The original "
            "local files remain preserved."
        )
        backup = (
            Path(record.store).parent / decode_migration_plan(raw, record).backup_directory_name
        )
        emit(f"Preserved files: {backup}")
        return completed
    return record
