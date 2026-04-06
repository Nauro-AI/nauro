"""Append-only validation log for debugging and tuning."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

VALIDATION_LOG_FILENAME = "validation-log.jsonl"


def log_validation(project_path: Path, proposal: dict, result: dict) -> None:
    """Append one JSON line to the validation log.

    Never crashes — silently swallows errors.
    """
    try:
        log_path = project_path / VALIDATION_LOG_FILENAME
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "proposal_title": proposal.get("title", ""),
            "proposal_rationale_preview": (proposal.get("rationale") or "")[:100],
            "tier": result.get("tier"),
            "status": result.get("status"),
            "operation": result.get("operation"),
            "similar_count": len(result.get("similar_decisions", [])),
            "conflicts_count": len(result.get("conflicts", [])),
            "assessment": result.get("assessment", ""),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.debug("failed to append validation log", exc_info=True)
