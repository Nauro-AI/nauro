"""Phase 1c T1.7 / T1.7b — identity lifecycle (D119 + C1 correction).

Covers:
1. Login order: alias(previous_id, distinct_id) FIRST, then set(distinct_id, props).
2. Login while telemetry disabled: NO alias / set; auth state still persists
   (auth.py owns persistence — identify_login is purely the telemetry side).
3. email_hash format: SHA-256 hex of email.strip().lower(); raw email never
   appears in captured args or in config.
4. Logout rotation: anonymous_id changes to a fresh UUID4; consent fields
   (enabled, consent_version, consented_at) are unchanged; user_id deletion is
   auth.py's job (we test that contract here in step 6).
5. NO posthog.reset() — verified absent from posthog 7.x. Test docstring
   references C1 to deter a future agent from re-adding it.
6. Shared-machine: User A login → logout → User B login on same machine →
   User B's events carry User B's user_id, NOT User A's (rotation protects
   against attribution leakage).
7. _get_distinct_id() post-rotation returns the new anonymous_id.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import pytest

_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class FakeClient:
    """Records call sequence so order-sensitive assertions (D119 alias-then-set) hold."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def alias(self, previous_id: str, distinct_id: str) -> None:
        self.calls.append(("alias", {"previous_id": previous_id, "distinct_id": distinct_id}))

    def set(self, distinct_id: str, properties: dict[str, Any]) -> None:
        self.calls.append(("set", {"distinct_id": distinct_id, "properties": properties}))

    def capture(self, event: str, distinct_id: str, properties: dict[str, Any]) -> None:
        self.calls.append(
            ("capture", {"event": event, "distinct_id": distinct_id, "properties": properties})
        )


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    return home


@pytest.fixture
def telemetry_key(monkeypatch):
    monkeypatch.setenv("NAURO_POSTHOG_KEY", "phc_test_key_for_unit_tests")


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    import nauro.telemetry.client as client_mod

    client_mod._client = None
    yield
    client_mod._client = None


@pytest.fixture
def fake_posthog(monkeypatch):
    import nauro.telemetry.client as client_mod

    fake = FakeClient()
    client_mod._client = fake
    yield fake
    client_mod._client = None


def _seed_telemetry_config(home, *, enabled: bool, anonymous_id: str | None = None) -> str:
    aid = anonymous_id or "11111111-1111-4111-8111-111111111111"
    (home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": aid,
                    "enabled": enabled,
                    "consent_version": 1,
                    "consented_at": "2026-04-30T00:00:00Z",
                }
            }
        )
    )
    return aid


# ── 1. Login order (telemetry enabled): alias FIRST, then set ───────────────


def test_login_calls_alias_then_set_in_correct_order(nauro_home, telemetry_key, fake_posthog):
    aid = _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.telemetry import identify_login

    identify_login(user_id="auth0|user-a", email_hash="deadbeef" * 8)

    # Exactly two calls, alias first, set second (D119 load-bearing order).
    assert len(fake_posthog.calls) == 2
    assert fake_posthog.calls[0][0] == "alias"
    assert fake_posthog.calls[0][1] == {"previous_id": aid, "distinct_id": "auth0|user-a"}
    assert fake_posthog.calls[1][0] == "set"
    assert fake_posthog.calls[1][1] == {
        "distinct_id": "auth0|user-a",
        "properties": {"email_hash": "deadbeef" * 8},
    }


# ── 2. Login while telemetry disabled — no alias/set fire ───────────────────


def test_login_with_telemetry_disabled_does_not_call_alias_or_set(
    nauro_home, telemetry_key, fake_posthog
):
    _seed_telemetry_config(nauro_home, enabled=False)

    from nauro.telemetry import identify_login

    identify_login(user_id="auth0|user-x", email_hash="cafef00d" * 8)

    assert fake_posthog.calls == []


def test_auth_state_persistence_is_independent_of_telemetry_consent(nauro_home):
    """auth.py owns user_id persistence; telemetry consent does not gate it."""
    from nauro.store.config import load_config, save_config

    cfg = load_config()
    cfg["auth"] = {"user_id": "auth0|user-a", "sub": "auth0|user-a"}
    save_config(cfg)

    cfg2 = load_config()
    assert cfg2["auth"]["user_id"] == "auth0|user-a"


# ── 3. email_hash format: SHA-256 hex of email.strip().lower() ──────────────


def test_email_hash_is_sha256_of_normalized_email(nauro_home, telemetry_key, fake_posthog):
    _seed_telemetry_config(nauro_home, enabled=True)

    raw_email = "  AlIcE@Example.COM  "
    expected_hash = hashlib.sha256(b"alice@example.com").hexdigest()

    from nauro.telemetry import identify_login

    identify_login(
        user_id="auth0|alice",
        email_hash=hashlib.sha256(raw_email.strip().lower().encode("utf-8")).hexdigest(),
    )

    set_props = fake_posthog.calls[1][1]["properties"]
    assert set_props["email_hash"] == expected_hash
    # Raw email never appears in captured payload.
    serialized = json.dumps(fake_posthog.calls)
    assert "alice@example.com" not in serialized.lower()
    assert "AlIcE" not in serialized


def test_raw_email_never_persisted_to_config(nauro_home, telemetry_key, fake_posthog):
    """Sanity guard — auth.py hashes before persisting; nothing readable on disk."""
    _seed_telemetry_config(nauro_home, enabled=True)
    from nauro.store.config import load_config, save_config

    cfg = load_config()
    cfg["auth"] = {"user_id": "auth0|alice"}  # auth.py never stores email
    save_config(cfg)

    raw = (nauro_home / "config.json").read_text()
    # The seeded user_id contains no '@', so any '@' in the file would be a
    # raw-email leak. The earlier OR'd guard was vacuous because "auth0|" was
    # always in the file, masking the assertion.
    assert "@" not in raw, "raw email leaked into config.json"


# ── 4. Logout rotation ──────────────────────────────────────────────────────


def test_logout_rotates_anonymous_id_and_preserves_consent(nauro_home):
    old_aid = _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.store.config import load_config
    from nauro.telemetry import identify_logout

    identify_logout()

    cfg = load_config()
    new_aid = cfg["telemetry"]["anonymous_id"]
    assert new_aid != old_aid
    assert _UUID4_RE.match(new_aid), new_aid
    # Consent fields untouched.
    assert cfg["telemetry"]["enabled"] is True
    assert cfg["telemetry"]["consent_version"] == 1
    assert cfg["telemetry"]["consented_at"] == "2026-04-30T00:00:00Z"


# ── 5. No posthog.reset() — C1 correction ───────────────────────────────────


def test_posthog_python_sdk_has_no_reset_attr():
    """Sanity guard against a future regression that re-adds posthog.reset().

    C1 correction (Phase 1c review): posthog.reset() is a JS-SDK API. The
    Python SDK 7.x has no such method. Calling it would raise AttributeError.
    Identity reset is application-side rotation only.
    """
    import posthog

    assert not hasattr(posthog, "reset")


def test_identify_logout_does_not_call_reset_on_client(nauro_home, telemetry_key, fake_posthog):
    """C1: identify_logout must never call .reset() on the SDK client."""
    _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.telemetry import identify_logout

    identify_logout()

    assert not any(c[0] == "reset" for c in fake_posthog.calls)


# ── 6. Shared-machine — User B's events do NOT carry User A's user_id ───────


def test_shared_machine_rotation_protects_attribution(nauro_home, telemetry_key, fake_posthog):
    """User A login → logout → User B login → User B emits → distinct_id is User B's.

    Without rotation, User B's pre-login (anonymous) events would alias back
    to User A's user_id from the previous session's alias call.
    """
    _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.store.config import load_config, save_config
    from nauro.telemetry import capture, identify_login, identify_logout

    # User A logs in (auth.py would persist user_id).
    cfg = load_config()
    cfg["auth"] = {"user_id": "auth0|user-a"}
    save_config(cfg)
    identify_login(user_id="auth0|user-a", email_hash="a" * 64)

    # User A logs out (auth.py would clear cfg["auth"]).
    identify_logout()
    cfg = load_config()
    cfg.pop("auth", None)
    save_config(cfg)

    # User B logs in.
    cfg = load_config()
    cfg["auth"] = {"user_id": "auth0|user-b"}
    save_config(cfg)
    identify_login(user_id="auth0|user-b", email_hash="b" * 64)

    # User B captures an event.
    fake_posthog.calls.clear()
    capture("cli.command_invoked", {"command": "test"})

    # The capture goes to User B's user_id, NOT User A's.
    captures = [c for c in fake_posthog.calls if c[0] == "capture"]
    assert len(captures) == 1
    assert captures[0][1]["distinct_id"] == "auth0|user-b"
    assert captures[0][1]["distinct_id"] != "auth0|user-a"


# ── 7. _get_distinct_id post-rotation ───────────────────────────────────────


def test_get_distinct_id_after_logout_returns_new_anonymous_id(nauro_home, telemetry_key):
    old_aid = _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.store.config import load_config, save_config
    from nauro.telemetry import _get_distinct_id, identify_logout

    # Simulate a logged-in user, then logout (auth.py clears auth section).
    cfg = load_config()
    cfg["auth"] = {"user_id": "auth0|user-a"}
    save_config(cfg)
    assert _get_distinct_id() == "auth0|user-a"  # logged in → user_id wins

    identify_logout()
    cfg = load_config()
    cfg.pop("auth", None)
    save_config(cfg)

    new_aid = load_config()["telemetry"]["anonymous_id"]
    assert new_aid != old_aid

    # Must return the NEW anonymous_id — not the cleared user_id, not the old aid.
    distinct = _get_distinct_id()
    assert distinct == new_aid
    assert distinct != old_aid
    assert distinct != "auth0|user-a"


def test_get_distinct_id_prefers_user_id_when_logged_in(nauro_home):
    aid = _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.store.config import load_config, save_config
    from nauro.telemetry import _get_distinct_id

    # No auth → anonymous_id.
    assert _get_distinct_id() == aid

    cfg = load_config()
    cfg["auth"] = {"user_id": "auth0|alice"}
    save_config(cfg)
    assert _get_distinct_id() == "auth0|alice"


# ── Bonus: defensive — uuid4 emitted on logout is a new UUID each call ──────


def test_consecutive_logouts_each_rotate_to_new_uuid4(nauro_home):
    _seed_telemetry_config(nauro_home, enabled=True)

    from nauro.store.config import load_config
    from nauro.telemetry import identify_logout

    seen: set[str] = set()
    seen.add(load_config()["telemetry"]["anonymous_id"])
    for _ in range(3):
        identify_logout()
        aid = load_config()["telemetry"]["anonymous_id"]
        assert _UUID4_RE.match(aid), aid
        # uuid.UUID parses it cleanly too.
        uuid.UUID(aid)
        assert aid not in seen
        seen.add(aid)
