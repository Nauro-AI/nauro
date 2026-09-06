from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from mcp.types import CallToolResult, TextContent
from nauro_core.renderers import disconnected_reason_code

from nauro.auth import ActiveUserReadError, read_active_user_id
from nauro.mcp import generation_responses as generation
from nauro.mcp import tools as legacy
from nauro.mcp.rendering import resolve_renderer_kwargs, try_render_envelope
from nauro.store.config import resolve_embeddings_flag
from nauro.store.generation_authority import GenerationAuthorityError
from nauro.store.read_authority import observe_generation_marker
from nauro.store.resolution import (
    ResolvedProjectBinding,
    StoreResolutionError,
    resolve_project_binding,
)
from nauro.sync.remote import TransferBoundaryError

logger = logging.getLogger(__name__)


def _prepared(response: generation.GenerationToolResponse) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=response.text)], isError=response.is_error
    )


def _unavailable() -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text="Error: Project read authority is unavailable.")],
        isError=True,
    )


def _legacy_result(
    name: str, envelope: dict[str, object], options: dict[str, object], path: Path
) -> CallToolResult:
    rendered = try_render_envelope(name, envelope, resolve_renderer_kwargs(name, options, path))
    if rendered.failure is not None:
        logger.error(
            "renderer failed for tool=%s; falling back to JSON-only",
            name,
            exc_info=rendered.failure,
        )
    text = rendered.text
    if text is None:
        text = json.dumps(envelope, indent=2, default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=envelope if disconnected_reason_code(envelope) is not None else None,
    )


def _read(
    name: str,
    project_id: str | None,
    cwd: str | None,
    flat: Callable[[Path], dict[str, object]],
    projected: Callable[[ResolvedProjectBinding, str], generation.GenerationToolResponse],
    options: dict[str, object] | None = None,
) -> CallToolResult:
    try:
        binding = resolve_project_binding(project_id, cwd)
        marker = observe_generation_marker(binding)
        if marker is not None:
            actor = read_active_user_id()
            response = projected(binding, actor)
            if read_active_user_id() != actor or observe_generation_marker(binding) != marker:
                return _unavailable()
            return _prepared(response)
        result = _legacy_result(name, flat(binding.store_path), options or {}, binding.store_path)
        if observe_generation_marker(binding) is not None:
            return _unavailable()
        return result
    except (
        StoreResolutionError,
        GenerationAuthorityError,
        ActiveUserReadError,
        TransferBoundaryError,
        OSError,
    ):
        return _unavailable()


def get_context(
    project_id: str | None = None, cwd: str | None = None, level: int | str = "L0"
) -> CallToolResult:
    return _read(
        "get_context",
        project_id,
        cwd,
        lambda p: legacy.tool_get_context(p, level),
        lambda b, a: generation.get_context(b, level, actor=a),
        {"level": level},
    )


def get_raw_file(
    path: str, project_id: str | None = None, cwd: str | None = None
) -> CallToolResult:
    return _read(
        "get_raw_file",
        project_id,
        cwd,
        lambda p: legacy.tool_get_raw_file(p, path),
        lambda b, a: generation.get_raw_file(b, path, actor=a),
        {"path": path},
    )


def list_decisions(
    project_id: str | None = None,
    cwd: str | None = None,
    limit: int = 20,
    include_superseded: bool = False,
) -> CallToolResult:
    return _read(
        "list_decisions",
        project_id,
        cwd,
        lambda p: legacy.tool_list_decisions(p, limit, include_superseded),
        lambda b, a: generation.list_decisions(b, limit, include_superseded, actor=a),
    )


def get_decision(
    number: int,
    mode: Literal["header", "full"] = "full",
    project_id: str | None = None,
    cwd: str | None = None,
) -> CallToolResult:
    return _read(
        "get_decision",
        project_id,
        cwd,
        lambda p: legacy.tool_get_decision(p, number, mode),
        lambda b, a: generation.get_decision(b, number, mode, actor=a),
        {"mode": mode},
    )


def search_decisions(
    query: str,
    limit: int = 10,
    include_superseded: bool = False,
    project_id: str | None = None,
    cwd: str | None = None,
) -> CallToolResult:
    return _read(
        "search_decisions",
        project_id,
        cwd,
        lambda p: legacy.tool_search_decisions(p, query, limit, include_superseded),
        lambda b, a: generation.search_decisions(
            b,
            query,
            limit,
            include_superseded,
            actor=a,
            use_embeddings=resolve_embeddings_flag(),
        ),
        {"query": query},
    )


def check_decision(
    proposed_approach: str,
    context: str | None = None,
    project_id: str | None = None,
    cwd: str | None = None,
) -> CallToolResult:
    return _read(
        "check_decision",
        project_id,
        cwd,
        lambda p: legacy.tool_check_decision(p, proposed_approach, context),
        lambda b, a: generation.check_decision(
            b, proposed_approach, context, actor=a, use_embeddings=resolve_embeddings_flag()
        ),
    )


def diff_since_last_session(
    project_id: str | None = None, cwd: str | None = None, days: int | None = None
) -> CallToolResult:
    return _read(
        "diff_since_last_session",
        project_id,
        cwd,
        lambda p: legacy.tool_diff_since_last_session(p, days),
        lambda b, a: generation.diff_since_last_session(),
    )
