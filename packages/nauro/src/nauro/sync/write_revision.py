"""Capture a replacement precondition without refreshing the writer's replica."""

from nauro_core.constants import STATE_CURRENT_FILENAME
from nauro_core.operations.update_state import compute_state_revision

from nauro.store.generation_authority import RefreshRequiredError
from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_refresh import _capture_prepared, _controls, _intent, _locked
from nauro.sync.generation_session import GenerationTransferSession


def capture_write_revision(
    binding: ResolvedProjectBinding,
    *,
    actor: str,
    session: GenerationTransferSession,
) -> str:
    with _locked(binding, actor, session) as paths:
        controls = _controls(paths)
        raw, intent = _intent(paths)
        if intent.classify(*controls) != "target_present":
            raise RefreshRequiredError("Complete replica recovery before preparing a write.")
    projection = _capture_prepared(binding, actor, session, controls, raw)
    if (
        projection.target.binding != binding
        or projection.target.identity.installed_for_user_id != actor
    ):
        raise RefreshRequiredError("The captured replica belongs to another writer.")
    path = STATE_CURRENT_FILENAME
    content = next(
        (artifact.content for artifact in projection.artifacts if artifact.path == path), None
    )
    return compute_state_revision(content)
