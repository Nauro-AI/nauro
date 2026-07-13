"""``get_context`` — assemble project context at L0/L1/L2 detail levels.

Cross-transport implementation: CLI, local stdio MCP, and remote HTTP MCP
all call this function with the same arguments and receive the same
:class:`GetContextResult`. Each transport's adapter wraps the call to add
transport-specific framing (``store`` field, telemetry emission, onboarding
sentinels, snapshot-diff trailers); the level dispatch and markdown
assembly are shared by construction.

Only the locked Store primitives are used: the snapshot/diff layer
and the "Last synced" trailer remain adapter concerns since they sit
outside the kernel's storage protocol.
"""

from __future__ import annotations

from nauro_core.constants import (
    OPEN_QUESTIONS_MD,
    PROJECT_MD,
    STACK_MD,
    STATE_CURRENT_FILENAME,
    STATE_HISTORY_FILENAME,
    STATE_MD,
)
from nauro_core.context import build_l0, build_l1, build_l2
from nauro_core.operations.decision_lookup import parse_all_decisions
from nauro_core.operations.results import ErrorPayload, GetContextResult
from nauro_core.operations.store import Store

# Builder dispatch keyed by level. Keeping this as a module-level dict
# rather than an if/elif keeps the level-set check (``level in _BUILDERS``)
# the single source of truth for valid levels.
_BUILDERS = {
    0: build_l0,
    1: build_l1,
    2: build_l2,
}


def _load_context_files(store: Store, level: int) -> dict[str, str]:
    """Load the markdown files the context builders consume.

    ``project.md`` is loaded at every level: L0 carries it as a stable-scope
    preamble (``build_l0`` skips content still in unedited scaffold form at
    composition time), and L1/L2 carry it verbatim. Each builder ignores keys
    it does not use, so files other than the state history are loaded
    unconditionally.
    """
    files: dict[str, str] = {}
    project = store.read_file(PROJECT_MD)
    if project is not None:
        files[PROJECT_MD] = project

    stack = store.read_file(STACK_MD)
    if stack is not None:
        files[STACK_MD] = stack

    questions = store.read_file(OPEN_QUESTIONS_MD)
    if questions is not None:
        files[OPEN_QUESTIONS_MD] = questions

    # Prefer state_current.md; fall back to state.md for pre-upgrade stores.
    # Truthy check (not ``is not None``) so a migration that left an empty
    # state_current.md placeholder still falls through to the populated
    # legacy state.md — this fallback contract keeps the payload
    # byte-identical across surfaces.
    current = store.read_file(STATE_CURRENT_FILENAME)
    if current:
        files[STATE_CURRENT_FILENAME] = current
    else:
        legacy = store.read_file(STATE_MD)
        if legacy is not None:
            files[STATE_MD] = legacy

    if level == 2:
        history = store.read_file(STATE_HISTORY_FILENAME)
        if history is not None:
            files[STATE_HISTORY_FILENAME] = history

    return files


def get_context(store: Store, level: int) -> GetContextResult:
    """Return assembled project context at the requested level.

    Args:
        store: Storage adapter providing the five locked primitives.
        level: Context tier — ``0`` (concise), ``1`` (working set), or
            ``2`` (full dump).

    Returns:
        :class:`GetContextResult`. On the success path ``content`` carries
        the assembled markdown. On the rejection path ``error`` is
        populated with ``kind="rejected"`` naming the offending level.
    """
    builder = _BUILDERS.get(level)
    if builder is None:
        return GetContextResult(
            error=ErrorPayload(kind="rejected", reason=f"Invalid level: {level}"),
        )

    files = _load_context_files(store, level)
    decisions = parse_all_decisions(store)
    return GetContextResult(content=builder(files, decisions))
