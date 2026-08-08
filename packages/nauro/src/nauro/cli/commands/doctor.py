"""nauro doctor — report deterministic store-integrity defects.

Reads the local store and reports structural defects in the decision set:
unparseable decision files, dangling or cyclic supersession refs, and status
contradictions. Report-only — it never edits the store — and it exits 0
whether or not it finds defects, because a defect is information for the user,
not a failed command.

Supersede backref orphans are reported in their own section and counted
separately: they are repairable rather than blocking, and the section names
`nauro repair` as the remedy. Doctor still writes nothing itself — detection
and the gated flip stay in different commands.

Scope boundary: doctor reads only the decision store, so its findings can be
deterministic with no false positives. Everything else that can be "off" on a
machine — not connected, missing or dead wiring — is `nauro status`'s job;
status names the remedy for each state (`nauro reconnect`, `nauro setup all`).
On a machine where the project has never been connected, doctor itself exits
through the shared resolution guidance rather than reporting on a store it
cannot read.
"""

from __future__ import annotations

import typer
from nauro_core.doctor import StoreDiagnosis, diagnose_store

from nauro.cli.utils import resolve_target_project
from nauro.store.filesystem_store import FilesystemStore


def _label(number: int) -> str:
    return f"D{number}"


def _render_unknown_keys(diagnosis: StoreDiagnosis) -> list[str]:
    """Render the advisory unknown-frontmatter-keys section, or nothing."""
    if not diagnosis.unknown_frontmatter_keys:
        return []
    lines = [
        f"Unknown frontmatter keys ({len(diagnosis.unknown_frontmatter_keys)}) "
        "(advisory, preserved on rewrite, not a defect):"
    ]
    for row in diagnosis.unknown_frontmatter_keys:
        lines.append(f"  {_label(row.number)}: {', '.join(row.keys)}")
    lines.append("")
    return lines


def _render_orphans(diagnosis: StoreDiagnosis) -> list[str]:
    """Render the repairable supersede-orphan section, or nothing."""
    if not diagnosis.supersede_orphans:
        return []
    lines = [
        f"Supersede backref orphans ({len(diagnosis.supersede_orphans)}) "
        "(repairable, not a blocking defect):"
    ]
    for row in diagnosis.supersede_orphans:
        lines.append(
            f"  {_label(row.child)} supersedes {_label(row.target)}, but "
            f"{_label(row.target)} is still active with no superseded_by backref"
        )
    lines.append("  Fix: run 'nauro repair' to review and confirm the backref.")
    lines.append("")
    return lines


def _render_repairable_total(diagnosis: StoreDiagnosis) -> list[str]:
    """Count repairable defects on their own line, apart from blocking ones."""
    if not diagnosis.supersede_orphans:
        return []
    return [f"Found {len(diagnosis.supersede_orphans)} repairable defect(s)."]


def _render_report(diagnosis: StoreDiagnosis) -> list[str]:
    """Render a diagnosis as human-readable report lines."""
    lines: list[str] = []

    if diagnosis.is_clean:
        return [
            "No integrity defects found.",
            *_render_orphans(diagnosis),
            *_render_unknown_keys(diagnosis),
            *_render_repairable_total(diagnosis),
        ]

    if diagnosis.unparseable:
        lines.append(f"Unparseable decision files ({len(diagnosis.unparseable)}):")
        for row in diagnosis.unparseable:
            lines.append(f"  {row.stem}: {row.error}")
        lines.append("")

    if diagnosis.dangling_refs:
        lines.append(f"Dangling supersession refs ({len(diagnosis.dangling_refs)}):")
        for ref in diagnosis.dangling_refs:
            lines.append(
                f"  {_label(ref.source)}.{ref.field} -> {_label(ref.target)} "
                "(no such decision on disk)"
            )
        lines.append("")

    if diagnosis.cycles:
        lines.append(f"Supersession cycles ({len(diagnosis.cycles)}):")
        for cycle in diagnosis.cycles:
            if len(cycle.members) == 1:
                lines.append(f"  {_label(cycle.members[0])} (self-reference)")
            else:
                lines.append("  " + " -> ".join(_label(n) for n in cycle.members))
        lines.append("")

    if diagnosis.contradictions:
        lines.append(f"Status contradictions ({len(diagnosis.contradictions)}):")
        for row in diagnosis.contradictions:
            if row.kind == "active_with_superseded_by":
                lines.append(
                    f"  {_label(row.decision)} is active but records "
                    f"superseded_by={_label(row.other)}"
                )
            else:
                lines.append(
                    f"  {_label(row.decision)} supersedes {_label(row.other)}, but "
                    f"{_label(row.other)} records "
                    f"superseded_by={_label(row.conflicting_with)}"
                )
        lines.append("")

    lines.extend(_render_orphans(diagnosis))
    lines.extend(_render_unknown_keys(diagnosis))

    total = (
        len(diagnosis.unparseable)
        + len(diagnosis.dangling_refs)
        + len(diagnosis.cycles)
        + len(diagnosis.contradictions)
    )
    lines.append(f"Found {total} defect(s).")
    lines.extend(_render_repairable_total(diagnosis))
    return lines


def doctor(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Target project name.",
    ),
) -> None:
    """Report deterministic integrity defects in the project's decision store.

    Checks only the store itself. For connection or wiring problems
    (not connected on this machine, missing or broken MCP wiring), run
    'nauro status', which names the remedy for each state.
    """
    project_name, store_path = resolve_target_project(project)

    diagnosis = diagnose_store(FilesystemStore(store_path))

    typer.echo(f"Project: {project_name}\n")
    for line in _render_report(diagnosis):
        typer.echo(line)
