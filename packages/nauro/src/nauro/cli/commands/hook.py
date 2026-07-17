"""Client-side context hooks for AI coding agents.

``user-prompt-submit`` surfaces related decisions to Claude Code on each turn.
``codex-bootstrap`` injects the canonical Nauro protocol and current L0 project
context when Codex starts a session or subagent.

The hooks never block a turn or write to the project store. Any failure,
including malformed stdin, an unresolved cwd, or an unreadable store, produces
no output and exits 0.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import typer
from nauro_core import MCP_INSTRUCTIONS_STATIC

from nauro.cli._codex_hooks import _CODEX_HOOK_EVENTS
from nauro.constants import DECISIONS_DIR, DEFAULT_NAURO_HOME, NAURO_HOME_ENV

hook_app = typer.Typer(help="Client-side advisory hooks for AI coding agents.")

# ── Tuning constants (initial values; final tuning deferred to the harness) ──

# BM25 relevance floor at the reference corpus size. A hit must clear the
# effective floor (see _effective_floor) to be injected. The BM25 score scales
# with corpus size and query length; against a few-hundred-decision corpus a
# strong terse-prompt match scores in the mid-teens, while weak near-neighbours
# sit in the low single digits. A floor here clears the weak tail and keeps
# genuine conflicts. Final tuning against field evidence is deferred.
RELEVANCE_FLOOR = 8.0

# Corpus size the absolute RELEVANCE_FLOOR is calibrated for. Because BM25 IDF
# (and thus the score scale) grows with corpus size, a fixed floor tuned for a
# large store silences every hit on a small one — e.g. the 7-decision
# `nauro init --demo` store tops out around 6, so an 8.0 floor would surface
# nothing, including the marquee websocket→SSE conflict. _effective_floor scales
# the floor down for smaller corpora; NAURO_HOOK_RELEVANCE_FLOOR overrides both.
RELEVANCE_FLOOR_REFERENCE_CORPUS = 200
RELEVANCE_FLOOR_ENV = "NAURO_HOOK_RELEVANCE_FLOOR"

# Maximum decisions injected per turn. Three is enough to surface the cluster
# around a conflict without flooding the context window.
MAX_INJECTED = 3

# Per-decision rationale preview length in the injected block. The kernel
# already caps rationale_preview at 200 chars; trim further for token economy.
PREVIEW_CHARS = 120

# Per-session dedup state: keep state files small and expire them so the
# hook-state directory does not grow without bound across long-lived machines.
SESSION_STATE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
MAX_SESSION_STATE_FILES = 500
MAX_DEDUP_ENTRIES_PER_SESSION = 200

# Injection copy. One advisory preamble line and one review instruction; the
# agent adjudicates the decision body itself.
_PREAMBLE = "Nauro: prior decisions may bear on this request - advisory only, not a block."
_INSTRUCTION = "Review these and call get_decision before acting on anything they constrain."

_CODEX_L0_HEADING = "## Nauro project context (L0)"


@hook_app.command(name="user-prompt-submit")
def user_prompt_submit() -> None:
    """Claude Code UserPromptSubmit hook: surface related decisions as context.

    Reads the hook payload JSON from stdin, resolves the project from the
    payload's ``cwd``, runs ``check_decision`` against the local store, and
    prints a ``hookSpecificOutput`` envelope with an advisory block when a
    decision clears the relevance floor and has not already surfaced this
    session. On any failure prints nothing and exits 0.
    """
    try:
        _run_user_prompt_submit()
    except Exception:
        # Fail-open by construction: any failure leaves the turn unblocked.
        pass
    raise typer.Exit(code=0)


@hook_app.command(name="codex-bootstrap")
def codex_bootstrap() -> None:
    """Inject Nauro protocol and L0 context into a Codex lifecycle event."""
    try:
        _run_codex_bootstrap()
    except Exception:
        pass
    raise typer.Exit(code=0)


def _run_codex_bootstrap() -> None:
    payload = json.loads(_read_stdin_utf8())
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or event_name not in _CODEX_HOOK_EVENTS:
        return

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return

    store_path = _resolve_store_path(Path(cwd))
    if store_path is None or not store_path.is_dir():
        return

    from nauro.mcp.payloads import build_l0_payload

    l0_payload = build_l0_payload(store_path)
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": _format_codex_bootstrap_context(l0_payload),
        }
    }
    sys.stdout.write(json.dumps(output))


def _format_codex_bootstrap_context(l0_payload: str) -> str:
    return f"{MCP_INSTRUCTIONS_STATIC}\n\n{_CODEX_L0_HEADING}\n\n{l0_payload}"


def _run_user_prompt_submit() -> None:
    payload = json.loads(_read_stdin_utf8())
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return
    session_id = payload.get("session_id")

    store_path = _resolve_store_path(Path(cwd))
    if store_path is None or not store_path.exists():
        return

    related = _check(store_path, prompt)
    if not related:
        return

    surviving = _apply_floor(related, _corpus_size(store_path))
    if not surviving:
        return

    already_seen = _load_seen(session_id)
    fresh = [r for r in surviving if r["number"] not in already_seen]
    if not fresh:
        return

    _enrich_supersedes(store_path, fresh)
    injected = _select_injected(fresh)
    block = _format_block(injected)
    _record_seen(session_id, [r["number"] for r in injected])

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }
    sys.stdout.write(json.dumps(output))


def _read_stdin_utf8() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    raw = buffer.read()
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _resolve_store_path(cwd: Path) -> Path | None:
    """Resolve the project store path from a hook payload's cwd.

    Delegates to the canonical ``resolve_from_cwd`` waterfall that
    ``cli/utils.resolve_target_project`` uses, but operates against the payload's
    cwd rather than the process cwd and returns None instead of raising — the
    hook never errors a turn over an unresolvable directory.
    """
    from nauro.store.resolution import RepoResolution, resolve_from_cwd

    resolution = resolve_from_cwd(cwd)
    return resolution.store_path if isinstance(resolution, RepoResolution) else None


def _check(store_path: Path, prompt: str) -> list[dict]:
    """Run the check_decision kernel and return its related-decision hits.

    The embeddings flag is resolved here so the hook shares the MCP and CLI
    retrieval path, but the MVP install does not set ``NAURO_EMBEDDINGS``, so the
    hook runs BM25-only by default. The flag wiring is retained for the follow-up
    that re-admits cosine-gated embedding hits once the kernel exposes the score.
    Each hit is reduced to the fields the injection block needs.
    """
    from nauro_core.operations.check_decision import check_decision
    from nauro_core.parsing import extract_decision_number

    from nauro.store.config import resolve_embeddings_flag
    from nauro.store.filesystem_store import FilesystemStore

    result = check_decision(
        FilesystemStore(store_path),
        prompt,
        use_embeddings=resolve_embeddings_flag(),
    )
    if result.error is not None:
        return []

    hits: list[dict] = []
    for rd in result.related_decisions:
        number = extract_decision_number(rd.id)
        if number is None:
            continue
        hits.append(
            {
                "number": number,
                "title": rd.title,
                "score": rd.score,
                "status": rd.status,
                "date": rd.date,
                "preview": rd.rationale_preview,
            }
        )
    return hits


def _corpus_size(store_path: Path) -> int:
    """Count decision files in the store, for corpus-size-aware floor scaling."""
    decisions_dir = store_path / DECISIONS_DIR
    if not decisions_dir.exists():
        return 0
    return sum(1 for _ in decisions_dir.glob("*.md"))


def _effective_floor(corpus_size: int) -> float:
    """Relevance floor adjusted for corpus size.

    An explicit ``NAURO_HOOK_RELEVANCE_FLOOR`` env value always wins. Otherwise
    the floor scales with ``log10(corpus_size)`` up to the reference corpus, so a
    small store (including the 7-decision ``--demo``) still surfaces a genuine
    conflict instead of clearing every hit, while a large store keeps the full
    floor that trims the weak tail. A single absolute floor cannot fit both
    because BM25 scores grow with corpus size.
    """
    override = os.environ.get(RELEVANCE_FLOOR_ENV)
    if override is not None:
        try:
            return float(override)
        except ValueError:
            pass
    if corpus_size >= RELEVANCE_FLOOR_REFERENCE_CORPUS:
        return RELEVANCE_FLOOR
    scale = math.log10(max(corpus_size, 2)) / math.log10(RELEVANCE_FLOOR_REFERENCE_CORPUS)
    return RELEVANCE_FLOOR * scale


def _apply_floor(hits: list[dict], corpus_size: int) -> list[dict]:
    """Filter hits to those worth injecting.

    BM25 hits at or above the corpus-adjusted relevance floor are kept in order;
    everything else is dropped. Embedding-only hits (score 0.0) are not admitted:
    on the validation corpus they recovered no conflict the BM25 floor missed
    while injecting a nearest-neighbour on every otherwise-silent prompt. The
    kernel discards the cosine score, so an embedding-only hit cannot be
    relevance-gated here. Re-admitting them is a follow-up that requires the
    kernel to expose the embedding score.
    """
    floor = _effective_floor(corpus_size)
    return [h for h in hits if h["score"] >= floor]


def _enrich_supersedes(store_path: Path, hits: list[dict]) -> None:
    """Attach resolved structural supersedes refs to injected hits, in place.

    For each hit, resolve whether the surfaced decision structurally supersedes
    another and, if so, that older decision's number and title, stored under the
    hit's ``supersedes_ref`` key. Resolution is fail-open per hit (see
    ``_resolve_supersedes_ref``): a hit that does not resolve is left without the
    key and renders the shipped line.
    """
    from nauro.store.filesystem_store import FilesystemStore

    store = FilesystemStore(store_path)
    for h in hits:
        ref = _resolve_supersedes_ref(store, h["number"])
        if ref is not None:
            h["supersedes_ref"] = ref


def _resolve_supersedes_ref(store, number: int) -> dict | None:
    """Resolve a surfaced decision's structural supersedes ref, or None.

    Returns ``{"number": old_num, "title": old_title}`` when the decision at
    ``number`` carries a ``supersedes`` ref that resolves to a readable,
    parseable decision on disk; otherwise None. Only a structural ref produces a
    payload: a decision with no ``supersedes`` resolves to None, so no relation
    wording is ever emitted without one.

    Fail-open per hit: a missing target, an unparseable file, or any OSError
    degrades to None so the hit renders the shipped line. Never propagates, since
    propagating would lose the whole advisory block through the caller's
    outer try/except.
    """
    from nauro_core.operations.decision_lookup import (
        find_decision_stem_by_num,
        parse_decision_or_none,
    )

    try:
        stem = find_decision_stem_by_num(store, number)
        if stem is None:
            return None
        body = store.read_decision(stem)
        if body is None:
            return None
        decision = parse_decision_or_none(body, f"{stem}.md")
        if decision is None or not decision.supersedes:
            return None
        old_number = int(decision.supersedes)
        old_stem = find_decision_stem_by_num(store, old_number)
        if old_stem is None:
            return None
        old_body = store.read_decision(old_stem)
        if old_body is None:
            return None
        old_decision = parse_decision_or_none(old_body, f"{old_stem}.md")
        if old_decision is None:
            return None
        return {"number": old_number, "title": old_decision.title}
    except Exception:
        return None


def _select_injected(candidates: list[dict]) -> list[dict]:
    """Select the injected hits, guaranteeing a slot for a superseding decision.

    Starts from the top ``MAX_INJECTED`` candidates by relevance. When none of
    them supersedes an older decision but a lower-ranked candidate does, the
    first such superseding hit takes the last slot, keeping the two strongest
    top hits. This guarantees the block always carries the top floor-clearing
    decision that supersedes another when one is present, at the cost of at most
    one relevance slot. Detection is structural: a hit carries a truthy
    ``supersedes_ref`` payload only when enrichment resolved a real supersedes
    relation on disk, so no relation is ever promoted without one.
    """
    injected = candidates[:MAX_INJECTED]
    if any(h.get("supersedes_ref") is not None for h in injected):
        return injected
    for h in candidates[MAX_INJECTED:]:
        if h.get("supersedes_ref") is not None:
            return injected[: MAX_INJECTED - 1] + [h]
    return injected


def _hit_meta(h: dict) -> str:
    """Render a hit's ``status`` or ``status, date`` metadata parenthetical."""
    meta = h["status"]
    if h["date"]:
        meta = f"{meta}, {h['date']}"
    return meta


def _trimmed_preview(h: dict) -> str:
    """Return the hit's rationale preview trimmed to the ``PREVIEW_CHARS`` cap."""
    preview = (h["preview"] or "").strip()
    if len(preview) > PREVIEW_CHARS:
        preview = preview[: PREVIEW_CHARS - 1].rstrip() + "…"
    return preview


def _shipped_line(h: dict) -> str:
    """Render the default hit line: id, quoted title, meta, then trimmed preview."""
    line = f'D{h["number"]:03d} "{h["title"]}" ({_hit_meta(h)})'
    preview = _trimmed_preview(h)
    if preview:
        line = f"{line} - {preview}"
    return line


def _explicit_line(h: dict, ref: dict) -> str:
    """Render a hit line that states the rejection of the superseded decision.

    Extends the shipped line's relation clause: instead of the bare preview, the
    line names the superseded decision and states that this decision rejected it
    in favor of the surfaced one, then appends the same trimmed preview.
    """
    old_title = ref["title"]
    new_title = h["title"]
    clause = (
        f'supersedes D{ref["number"]:03d} "{old_title}": '
        f'this decision rejected "{old_title}" in favor of "{new_title}".'
    )
    line = f'D{h["number"]:03d} "{new_title}" ({_hit_meta(h)}) - {clause}'
    preview = _trimmed_preview(h)
    if preview:
        line = f"{line} {preview}"
    return line


def _format_block(hits: list[dict]) -> str:
    """Render the advisory block: a preamble, one line per hit, an instruction.

    A hit whose enrichment resolved a structural ``supersedes`` ref renders the
    explicit rejection line; every other hit renders the shipped line, byte for
    byte as before, so no get_decision call is needed.
    """
    lines = [_PREAMBLE]
    for h in hits:
        ref = h.get("supersedes_ref")
        lines.append(_explicit_line(h, ref) if ref is not None else _shipped_line(h))
    lines.append(_INSTRUCTION)
    return "\n".join(lines)


# ── Per-session dedup state ──────────────────────────────────────────────────


def _hook_state_dir() -> Path:
    nauro_home = Path(os.environ.get(NAURO_HOME_ENV, Path.home() / DEFAULT_NAURO_HOME))
    return nauro_home / "hook-state"


def _session_state_file(session_id: str) -> Path | None:
    """Return the state file path for a session, or None when unusable.

    The session id comes from an external payload, so reject anything that is
    not a plain identifier rather than letting it shape a filesystem path.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    if "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        return None
    return _hook_state_dir() / f"{session_id}.json"


def _load_seen(session_id: str | None) -> set[int]:
    """Return the decision numbers already surfaced this session.

    Fail-open: an unreadable or malformed state file means we inject anyway, so
    a corrupt dedup record never silences a genuine conflict.
    """
    if session_id is None:
        return set()
    state_file = _session_state_file(session_id)
    if state_file is None or not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text())
        seen = data.get("seen", [])
        return {int(n) for n in seen}
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return set()


def _record_seen(session_id: str | None, numbers: list[int]) -> None:
    """Persist newly-surfaced decision numbers for this session.

    Best-effort: an unwritable state directory is swallowed, so dedup degrades
    to per-invocation but the turn still completes.
    """
    if session_id is None:
        return
    state_file = _session_state_file(session_id)
    if state_file is None:
        return
    try:
        existing = _load_seen(session_id)
        merged = sorted(existing | set(numbers))[-MAX_DEDUP_ENTRIES_PER_SESSION:]
        state_file.parent.mkdir(parents=True, exist_ok=True)
        _prune_state_dir(state_file.parent)
        payload = {"seen": merged, "updated_at": time.time()}
        state_file.write_text(json.dumps(payload))
    except OSError:
        return


def _prune_state_dir(state_dir: Path) -> None:
    """Drop expired and surplus session-state files to bound the directory.

    Removes files older than the TTL, then trims to the most-recent cap by
    mtime. Best-effort: individual unlink failures are ignored.
    """
    try:
        files = [p for p in state_dir.glob("*.json") if p.is_file()]
    except OSError:
        return
    now = time.time()
    survivors: list[tuple[float, Path]] = []
    for p in files:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if now - mtime > SESSION_STATE_TTL_SECONDS:
            _unlink_quiet(p)
            continue
        survivors.append((mtime, p))
    if len(survivors) <= MAX_SESSION_STATE_FILES:
        return
    survivors.sort(key=lambda t: t[0])
    for _mtime, p in survivors[: len(survivors) - MAX_SESSION_STATE_FILES]:
        _unlink_quiet(p)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
