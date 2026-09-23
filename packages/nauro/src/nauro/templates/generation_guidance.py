"""Report derived guidance separately from a completed replica refresh."""

from typing import Any

from nauro.store.generation_store import GenerationSnapshotStore
from nauro.templates.agents_md_regen import warn_then_regen


def regenerate_refreshed_guidance(snapshot: GenerationSnapshotStore) -> dict[str, Any]:
    binding = snapshot.target.binding
    warnings: list[str] = []
    try:
        updated = warn_then_regen(
            binding.project_id, binding.store_path, warn=warnings.append, snapshot=snapshot
        )
    except (OSError, ValueError):
        return {
            "status": "failed",
            "message": "Replica refresh completed, but AGENTS.md regeneration failed. "
            "Check authorization and file permissions, then run 'nauro sync'. "
            "Do not resubmit the decision.",
        }
    return {"status": "completed", "updated_repos": len(updated), "warnings": warnings}
