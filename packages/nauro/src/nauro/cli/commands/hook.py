"""nauro hook — client-side advisory hooks for AI coding agents.

Currently implements the Claude Code ``UserPromptSubmit`` hook. On each turn
Claude Code invokes ``nauro hook user-prompt-submit`` with the hook payload on
stdin; this command runs the ``check_decision`` kernel against the local store
and, when a relevant decision clears the relevance floor, emits a compact
advisory block via ``hookSpecificOutput.additionalContext``.

The hook is advisory by construction: it never blocks a turn, never writes to
the store, and never exits non-zero. Any failure — malformed stdin, no project
for the cwd, a missing store, a missing optional embeddings extra — is swallowed
and the command prints nothing and exits 0. A turn must never be blocked by
Nauro.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import typer

from nauro.constants import DECISIONS_DIR, DEFAULT_NAURO_HOME, NAURO_HOME_ENV

hook_app = typer.Typer(help="Client-side advisory hooks for AI coding agents.")

# ── Tuning constants (initial values; final tuning deferred to the harness) ──

# BM25 relevance floor at the reference corpus size. A hit must clear the
# effective floor (see _effective_floor) to be injected. The BM25 score scales
# with corpus size and query length; against a few-hundred-decision corpus a
# strong terse-prompt match scores in the mid-teens, while weak near-neighbours
# sit in the low single digits. A floor here clears the weak tail and keeps
# genuine conflicts. Final tuning against field telemetry is deferred.
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
_PREAMBLE = "Nauro: prior decisions may bear on this request — advisory only, not a block."
_INSTRUCTION = "Review these and call get_decision before acting on anything they constrain."


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


def _run_user_prompt_submit() -> None:
    payload = json.loads(sys.stdin.read())
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

    injected = fresh[:MAX_INJECTED]
    block = _format_block(injected)
    _record_seen(session_id, [r["number"] for r in injected])

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }
    sys.stdout.write(json.dumps(output))


def _resolve_store_path(cwd: Path) -> Path | None:
    """Resolve the project store path from a hook payload's cwd.

    Reuses the per-repo config walk-up and the v2 registry path resolution that
    ``cli/utils.resolve_target_project`` uses, but operates against the payload's
    cwd rather than the process cwd and returns None instead of raising — the
    hook never errors a turn over an unresolvable directory.
    """
    from nauro.store.registry import get_store_path_v2, resolve_v2_from_path
    from nauro.store.repo_config import (
        RepoConfigSchemaError,
        find_repo_config,
        load_repo_config,
    )

    config_path = find_repo_config(cwd)
    if config_path is not None:
        repo_root = config_path.parent.parent
        try:
            cfg = load_repo_config(repo_root)
        except (RepoConfigSchemaError, OSError):
            cfg = None
        if cfg is not None:
            return get_store_path_v2(cfg["id"])

    v2_match = resolve_v2_from_path(cwd)
    if v2_match is not None:
        pid, _entry = v2_match
        return get_store_path_v2(pid)
    return None


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


def _format_block(hits: list[dict]) -> str:
    """Render the advisory block: a preamble, one line per hit, an instruction.

    Each hit line is ``D### "title" (status, date) — <preview>`` using the
    rationale preview already on the hit, so no get_decision call is needed.
    """
    lines = [_PREAMBLE]
    for h in hits:
        preview = (h["preview"] or "").strip()
        if len(preview) > PREVIEW_CHARS:
            preview = preview[: PREVIEW_CHARS - 1].rstrip() + "…"
        meta = h["status"]
        if h["date"]:
            meta = f"{meta}, {h['date']}"
        line = f'D{h["number"]:03d} "{h["title"]}" ({meta})'
        if preview:
            line = f"{line} — {preview}"
        lines.append(line)
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
