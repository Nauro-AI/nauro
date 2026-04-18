"""LLM extraction pipeline — extract structured context from commits.

Uses the Anthropic SDK with tool_use for structured output. Calls Haiku
to classify commits and extract decisions, questions, and state deltas.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from nauro.constants import (
    DEFAULT_SIGNAL_THRESHOLD,
    EXTRACTION_LOG_FILENAME,
    NAURO_SIGNAL_THRESHOLD_ENV,
)
from nauro.extraction.anthropic_provider import AnthropicProvider

# Re-export for backward compatibility (moved to prompts.py)
from nauro.extraction.prompts import EXTRACTION_TOOL  # noqa: F401
from nauro.extraction.providers import ExtractionProvider
from nauro.extraction.types import ExtractionOutcome, ExtractionResult, ExtractionSkipped
from nauro.store import reader, registry, writer
from nauro.templates.agents_md import regenerate_agents_md_for_project

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction log — append-only JSONL for debugging/tuning
# ---------------------------------------------------------------------------


def _append_extraction_log(store_path: Path, entry: dict) -> None:
    """Append a JSON line to the extraction log.

    The log is at ~/.nauro/projects/<name>/extraction-log.jsonl.
    Never crashes — silently swallows errors.
    """
    try:
        log_path = store_path / EXTRACTION_LOG_FILENAME
        entry["timestamp"] = datetime.now(UTC).isoformat()
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.debug("Failed to write extraction log", exc_info=True)


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------


def _make_skip_result() -> dict:
    """Return a default skip result with the new schema."""
    return {
        "decisions": [],
        "questions": [],
        "state_delta": None,
        "signal": {
            "architectural_significance": 0.0,
            "novelty": 0.0,
            "rationale_density": 0.0,
            "reversibility": 0.0,
            "scope": 0.0,
        },
        "composite_score": 0.0,
        "skip": True,
        "reasoning": "",
    }


def _make_no_api_key_result() -> dict:
    """Return a skip result for when no API key is configured."""
    return {
        "decisions": [],
        "questions": [],
        "state_delta": None,
        "signal": {
            "architectural_significance": 0.0,
            "novelty": 0.0,
            "rationale_density": 0.0,
            "reversibility": 0.0,
            "scope": 0.0,
        },
        "composite_score": None,
        "skip": True,
        "reasoning": "no_api_key",
    }


def _has_api_key(api_key: str | None = None) -> bool:
    """Check if an Anthropic API key is available."""
    return bool(api_key or os.environ.get("ANTHROPIC_API_KEY"))


def _show_no_api_key_hint() -> None:
    """Print a one-time hint about missing API key. Uses a sentinel file to fire exactly once."""
    nauro_home = Path(os.environ.get("NAURO_HOME", Path.home() / ".nauro"))
    hints_file = nauro_home / ".hints"
    sentinel = "no_api_key_hint_shown"

    try:
        existing = hints_file.read_text() if hints_file.exists() else ""
        if sentinel in existing:
            return
        import sys

        print(
            "Nauro: LLM extraction inactive.\n"
            "  Run `nauro config set anthropic_api_key <key>` to enable automatic capture.",
            file=sys.stderr,
        )
        hints_file.parent.mkdir(parents=True, exist_ok=True)
        with open(hints_file, "a") as f:
            f.write(sentinel + "\n")
    except Exception:
        pass  # Never crash the hook


def extract_from_commit(
    commit_message: str,
    diff_summary: str,
    changed_files: list[str],
    api_key: str | None = None,
    existing_decisions: list[str] | None = None,
    provider: ExtractionProvider | None = None,
) -> ExtractionOutcome:
    """Extract structured context from a commit via an ExtractionProvider.

    Args:
        commit_message: The git commit message.
        diff_summary: Output of git diff --stat (or similar).
        changed_files: List of changed file paths.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        existing_decisions: Optional list of existing decision titles for dedup.
        provider: ExtractionProvider to use. Defaults to AnthropicProvider.

    Returns:
        ExtractionResult on success, ExtractionSkipped if no API key or on error.
    """
    if provider is None:
        provider = AnthropicProvider(api_key=api_key)

    return provider.extract_from_diff(
        commit_message, diff_summary, changed_files, existing_decisions
    )


# ---------------------------------------------------------------------------
# Git info helpers
# ---------------------------------------------------------------------------


def get_commit_info(
    repo_path: str,
    commit_ref: str | None = None,
) -> tuple[str, str, list[str]]:
    """Read a commit's message, diff summary, and changed files.

    Args:
        repo_path: Path to the git repository root.
        commit_ref: Specific commit hash or ref. Defaults to HEAD.

    Returns:
        (commit_message, diff_summary, changed_files)
    """
    ref = commit_ref or "HEAD"
    parent = f"{ref}~1"
    run_opts = {"cwd": repo_path, "capture_output": True, "text": True, "timeout": 10}

    commit_message = subprocess.run(  # type: ignore[call-overload]
        ["git", "log", "-1", "--format=%s", ref], **run_opts
    ).stdout.strip()

    diff_summary = subprocess.run(  # type: ignore[call-overload]
        ["git", "diff", parent, ref, "--stat"], **run_opts
    ).stdout.strip()

    changed_raw = subprocess.run(  # type: ignore[call-overload]
        ["git", "diff", parent, ref, "--name-only"], **run_opts
    ).stdout.strip()
    changed_files = [f for f in changed_raw.split("\n") if f]

    return commit_message, diff_summary, changed_files


# ---------------------------------------------------------------------------
# Full pipeline: extract → route to store
# ---------------------------------------------------------------------------


def process_commit(
    repo_path: str,
    store_path: str | Path,
    api_key: str | None = None,
    threshold: float | None = None,
) -> ExtractionResult | None:
    """Extract context from the latest commit and write to the project store.

    Args:
        repo_path: Path to the git repository.
        store_path: Path to the project store directory.
        api_key: Anthropic API key (optional, falls back to env).
        threshold: Minimum composite_score to write. Defaults to
            NAURO_SIGNAL_THRESHOLD env var or 0.4.

    Returns:
        The ExtractionResult if content was written, None if skipped.
    """
    if threshold is None:
        threshold = float(os.environ.get(NAURO_SIGNAL_THRESHOLD_ENV, str(DEFAULT_SIGNAL_THRESHOLD)))

    store_path = Path(store_path)
    commit_message, diff_summary, changed_files = get_commit_info(repo_path)

    if not commit_message:
        return None

    # Load existing decision titles for dedup awareness
    try:
        existing_titles = [d["title"] for d in reader.list_active_decisions(store_path)]
    except Exception:
        existing_titles = None

    outcome = extract_from_commit(
        commit_message,
        diff_summary,
        changed_files,
        api_key=api_key,
        existing_decisions=existing_titles,
    )

    # Handle skipped extractions (no API key, error, no tool_use)
    if isinstance(outcome, ExtractionSkipped):
        _append_extraction_log(
            store_path,
            {
                "source": "commit",
                "commit_message": commit_message,
                "signal": {},
                "composite_score": None,
                "skip": True,
                "reasoning": outcome.reason,
                "captured": False,
            },
        )
        if outcome.reason == "no_api_key":
            _show_no_api_key_hint()
        return None

    result = outcome
    signal = result.signal

    # Log every extraction attempt (captured or skipped)
    _append_extraction_log(
        store_path,
        {
            "source": "commit",
            "commit_message": commit_message,
            "signal": signal.to_dict(),
            "composite_score": signal.composite_score,
            "skip": result.skip,
            "reasoning": signal.reasoning,
            "captured": not result.skip and signal.composite_score >= threshold,
        },
    )

    if result.skip or signal.composite_score < threshold:
        return None

    # Route extracted content through the validation pipeline
    route_extraction_to_store(
        result.to_dict(),
        store_path,
        source="commit",
        trigger=f"extract: {commit_message[:80]}",
    )

    # Regenerate AGENTS.md in all associated repos so context stays current
    project_name = registry.resolve_project(Path(repo_path))
    if project_name:
        regenerate_agents_md_for_project(project_name, store_path)

        # Push to S3 after extraction (event-driven sync)
        try:
            from nauro.sync.hooks import push_after_extraction

            push_after_extraction(project_name, store_path)
        except Exception:
            # Log but never block the hook
            _append_extraction_log(
                store_path,
                {
                    "source": "commit",
                    "event": "push_failed",
                    "reasoning": "s3_push_error",
                },
            )

    return result


def route_extraction_to_store(
    result: dict,
    store_path: Path,
    source: str = "compaction",
    session_id: str | None = None,
    trigger: str | None = None,
) -> dict | None:
    """Route an extraction result to the project store through validation.

    Shared logic between commit extraction and session extraction.
    Does NOT check threshold — caller is responsible for that.
    All decisions are routed through the validation pipeline with auto_confirm=True.

    Args:
        result: Parsed extraction result dict.
        store_path: Path to the project store directory.
        source: Extraction source for attribution.
        session_id: Optional session ID for source attribution.
        trigger: Snapshot trigger message.

    Returns:
        The result dict if content was written, None if nothing to write.
    """
    from nauro.validation.pipeline import validate_proposed_write

    source_label = source
    if session_id:
        source_label = f"{source} (session {session_id})"

    wrote_anything = False

    for decision in result.get("decisions", []):
        rejected = decision.get("rejected")
        rejected_alternatives: list[dict] | None = None
        if rejected:
            rejected_alternatives = []
            for item in rejected:
                if isinstance(item, dict):
                    # Skip entries with no usable reason — the v2 Decision
                    # validator rejects reasonless rejections on active
                    # decisions. Feeding them through would fail-loudly at
                    # the write step anyway; dropping here prevents the
                    # whole proposal from being lost when the LLM returns a
                    # partially-structured payload.
                    alt_name = item.get("alternative") or item.get("name")
                    reason = (item.get("reason") or "").strip()
                    if not alt_name or not reason:
                        logger.debug(
                            "dropping rejected alternative without name+reason: %r",
                            item,
                        )
                        continue
                    rejected_alternatives.append({"alternative": alt_name, "reason": reason})
                elif isinstance(item, str):
                    # Bare strings from the extractor have no reason attached.
                    # Drop them rather than fabricate one; the validator would
                    # reject the whole proposal otherwise.
                    logger.debug("dropping bare-string rejected alternative: %r", item)

        proposal = {
            "title": decision.get("title", "Untitled"),
            "rationale": decision.get("rationale"),
            "rejected": rejected_alternatives,
            "confidence": decision.get("confidence", "medium"),
            "decision_type": decision.get("decision_type"),
            "reversibility": decision.get("reversibility"),
            "files_affected": decision.get("files_affected"),
            "source": source_label,
        }

        validation = validate_proposed_write(proposal, store_path, auto_confirm=True)

        if validation.status in ("confirmed",):
            wrote_anything = True
        # rejected, noop — skip silently (already logged by validation pipeline)

    for question in result.get("questions", []):
        writer.append_question(store_path, question)
        wrote_anything = True

    state_delta = result.get("state_delta")
    if state_delta:
        writer.update_state(store_path, state_delta)
        wrote_anything = True

    if wrote_anything:
        # Snapshot is already captured by the validation pipeline for decisions,
        # but we need one for questions/state if no decisions were written
        return result

    return None
