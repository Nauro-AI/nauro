"""Drift tests for the canonical Nauro skill bodies.

The canonical body lives at ``packages/nauro/src/nauro/skills/{adopt,session}_body.md``.
``load_adopt_body()`` / ``load_session_body()`` return that body via importlib.resources.

Per-surface dogfood files (Claude Code, Cursor, Codex) and their containment
tests land in PR-B2 alongside ``nauro adopt`` and the surface materialization
handlers — there's no point committing surface-discoverable skill files until
the ``nauro adopt`` command they reference exists.
"""

from __future__ import annotations

from nauro.skills import load_adopt_body, load_session_body


def test_load_adopt_body_returns_canonical_bytes():
    body = load_adopt_body()
    assert body.endswith("\n")
    assert 1000 < len(body) < 20000
    # Anchor on key step markers — catches accidental empty / corrupted body.
    assert "Step 1 — Detect repo root" in body
    assert "Step 5a — Clear decisions" in body
    assert "Step 10 — Summary" in body


def test_load_session_body_returns_canonical_bytes():
    body = load_session_body()
    assert body.endswith("\n")
    assert 500 < len(body) < 5000
    assert "call get_context" in body
    assert "call check_decision" in body
    assert "call update_state" in body
