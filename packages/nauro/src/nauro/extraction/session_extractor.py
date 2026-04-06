"""Session-level extraction — extract decisions from Claude Code sessions.

Primary path: extract from compaction summaries (pre-filtered, structured).
Fallback: extract from raw session JSONL files (more expensive, lower quality).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

try:
    import anthropic
except ImportError:
    raise ImportError("anthropic package required for extraction: pip install nauro[extraction]")

from nauro.constants import (
    DEFAULT_EXTRACTION_MODEL,
    NAURO_EXTRACTION_MODEL_ENV,
)
from nauro.extraction.pipeline import (
    _has_api_key,
    _make_no_api_key_result,
    _make_skip_result,
)
from nauro.extraction.prompts import (
    COMPACTION_EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_TOOL,
    build_compaction_extraction_prompt,
)
from nauro.extraction.signal import from_dict

logger = logging.getLogger(__name__)


def extract_from_compaction(
    compaction_summary: str,
    project_path: Path,
    session_id: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Extract structured decisions from a compaction summary.

    This is the primary extraction path for session-level extraction.
    The compaction summary already contains key technical decisions — we
    just need to structure them.

    Args:
        compaction_summary: The compaction summary text from Claude Code.
        project_path: Path to the project store (for logging).
        session_id: Optional session ID for attribution.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        Parsed extraction result dict (same format as extract_from_commit).
    """
    if not _has_api_key(api_key):
        return _make_no_api_key_result()

    skip_result = _make_skip_result()

    if not compaction_summary or not compaction_summary.strip():
        return skip_result

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get(NAURO_EXTRACTION_MODEL_ENV, DEFAULT_EXTRACTION_MODEL)
        user_prompt = build_compaction_extraction_prompt(compaction_summary)

        response = client.messages.create(  # type: ignore[call-overload]
            model=model,
            max_tokens=1024,
            system=COMPACTION_EXTRACTION_SYSTEM_PROMPT,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_extraction"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "record_extraction":
                result = block.input
                result.setdefault("decisions", [])
                result.setdefault("questions", [])
                result.setdefault("state_delta", None)
                result.setdefault(
                    "signal",
                    {
                        "architectural_significance": 0.0,
                        "novelty": 0.0,
                        "rationale_density": 0.0,
                        "reversibility": 0.0,
                        "scope": 0.0,
                    },
                )
                result.setdefault("composite_score", 0.0)
                result.setdefault("skip", True)
                result.setdefault("reasoning", "")

                # Set source on all decisions
                for decision in result["decisions"]:
                    decision.setdefault("source", "compaction")

                return result  # type: ignore[no-any-return]

        return skip_result

    except Exception:
        logger.debug("compaction extraction failed", exc_info=True)
        return skip_result


def extract_from_session_jsonl(
    session_path: Path,
    project_path: Path,
    api_key: str | None = None,
) -> dict:
    """Fallback extraction from a raw Claude Code session JSONL file.

    Reads the session transcript, chunks it, runs extraction on each chunk,
    and deduplicates decisions. More expensive and lower quality than
    compaction extraction.

    Args:
        session_path: Path to the session JSONL file.
        project_path: Path to the project store.
        api_key: Anthropic API key.

    Returns:
        Merged extraction result dict.
    """
    skip_result = _make_skip_result()

    if not session_path.exists():
        return skip_result

    try:
        transcript = _parse_session_jsonl(session_path)
    except Exception:
        logger.debug("session JSONL parse failed for %s", session_path, exc_info=True)
        return skip_result

    if not transcript:
        return skip_result

    chunks = _chunk_transcript(transcript, max_tokens=5000)
    all_decisions = []
    all_questions = []
    state_deltas = []
    best_signal = None
    best_composite = 0.0
    best_reasoning = ""

    for chunk in chunks:
        result = _extract_from_chunk(chunk, api_key=api_key)
        if result.get("skip"):
            continue

        signal = from_dict(result)
        if signal.composite_score > best_composite:
            best_composite = signal.composite_score
            best_signal = result.get("signal")
            best_reasoning = signal.reasoning

        all_decisions.extend(result.get("decisions", []))
        all_questions.extend(result.get("questions", []))
        delta = result.get("state_delta")
        if delta:
            state_deltas.append(delta)

    # Deduplicate decisions by title similarity
    deduped = _deduplicate_decisions(all_decisions)
    deduped_questions = list(dict.fromkeys(all_questions))

    if not deduped and not deduped_questions and not state_deltas:
        return skip_result

    return {
        "decisions": deduped,
        "questions": deduped_questions,
        "state_delta": state_deltas[-1] if state_deltas else None,
        "signal": best_signal or skip_result["signal"],
        "composite_score": best_composite,
        "skip": False,
        "reasoning": best_reasoning,
    }


def _parse_session_jsonl(session_path: Path) -> str:
    """Parse a Claude Code session JSONL file into a conversation transcript.

    Defensive — handles missing fields, unexpected types, malformed lines.

    Args:
        session_path: Path to the JSONL file.

    Returns:
        Concatenated transcript string.
    """
    lines_out = []
    for raw_line in session_path.read_text().splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

        # Handle content as string or list of content blocks
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[tool: {block.get('name', '?')}]")
                elif isinstance(block, str):
                    text_parts.append(block)
            content = " ".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)

        if content.strip():
            lines_out.append(f"{role}: {content.strip()}")

    return "\n".join(lines_out)


def _chunk_transcript(transcript: str, max_tokens: int = 5000) -> list[str]:
    """Chunk a transcript at message boundaries, roughly max_tokens per chunk.

    Uses a simple heuristic: ~4 chars per token.
    """
    max_chars = max_tokens * 4
    lines = transcript.split("\n")
    chunks = []
    current_chunk: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line)
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks


def _extract_from_chunk(chunk: str, api_key: str | None = None) -> dict:
    """Run extraction on a single transcript chunk."""
    if not _has_api_key(api_key):
        return _make_no_api_key_result()

    skip_result = _make_skip_result()
    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get(NAURO_EXTRACTION_MODEL_ENV, DEFAULT_EXTRACTION_MODEL)

        response = client.messages.create(  # type: ignore[call-overload]
            model=model,
            max_tokens=1024,
            system=COMPACTION_EXTRACTION_SYSTEM_PROMPT,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_extraction"},
            messages=[{"role": "user", "content": build_compaction_extraction_prompt(chunk)}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "record_extraction":
                result = block.input
                result.setdefault("decisions", [])
                result.setdefault("questions", [])
                result.setdefault("state_delta", None)
                result.setdefault("signal", skip_result["signal"])
                result.setdefault("composite_score", 0.0)
                result.setdefault("skip", True)
                result.setdefault("reasoning", "")
                return result  # type: ignore[no-any-return]

        return skip_result
    except Exception:
        logger.debug("chunk extraction failed", exc_info=True)
        return skip_result


def _deduplicate_decisions(decisions: list[dict]) -> list[dict]:
    """Deduplicate decisions by title similarity.

    Simple approach: normalize titles and skip exact duplicates.
    """
    seen_titles: set[str] = set()
    deduped = []
    for d in decisions:
        normalized = d.get("title", "").lower().strip()
        if normalized and normalized not in seen_titles:
            seen_titles.add(normalized)
            deduped.append(d)
    return deduped


def find_session_jsonl(session_id: str, cwd: str | None = None) -> Path | None:
    """Find a Claude Code session JSONL file.

    Searches in ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl.
    If cwd is not provided, searches all project directories.

    Args:
        session_id: The session ID to find.
        cwd: Optional working directory to narrow the search.

    Returns:
        Path to the session file, or None if not found.
    """
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None

    # If cwd provided, try the encoded path first
    if cwd:
        encoded = cwd.replace("/", "-").lstrip("-")
        project_dir = claude_dir / encoded
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate

    # Search all project directories
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate

    return None


def read_compaction_from_session(session_path: Path) -> str | None:
    """Read the most recent compaction block from a session JSONL file.

    Looks for messages with type "summary" or role "system" containing
    compaction content.

    Args:
        session_path: Path to the session JSONL file.

    Returns:
        The compaction summary text, or None if not found.
    """
    if not session_path.exists():
        return None

    last_compaction = None
    for raw_line in session_path.read_text().splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        # Look for compaction/summary messages
        msg_type = msg.get("type", "")
        if msg_type in ("summary", "compaction"):
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                last_compaction = content.strip()
            continue

        # Also check for system messages that look like compaction
        role = msg.get("role", "")
        if role == "system":
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if "summary" in text.lower()[:100] or "compaction" in text.lower()[:100]:
                            last_compaction = text.strip()
            elif isinstance(content, str) and content.strip():
                if "summary" in content.lower()[:100] or "compaction" in content.lower()[:100]:
                    last_compaction = content.strip()

    return last_compaction
