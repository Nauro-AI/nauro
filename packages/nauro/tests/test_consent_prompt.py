"""Tests for nauro.telemetry.consent.maybe_prompt."""

from __future__ import annotations

import json
from datetime import datetime

import pytest


@pytest.fixture
def nauro_home(tmp_path, monkeypatch):
    home = tmp_path / ".nauro"
    home.mkdir()
    monkeypatch.setenv("NAURO_HOME", str(home))
    return home


def _set_tty(monkeypatch, value: bool) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: value)
    monkeypatch.setattr("sys.stdout.isatty", lambda: value)


def _set_input(monkeypatch, response: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": response)


def _read_telemetry(home) -> dict:
    cf = home / "config.json"
    if not cf.exists():
        return {}
    return json.loads(cf.read_text()).get("telemetry", {})


def _seed(home, *, enabled, consent_version) -> None:
    (home / "config.json").write_text(
        json.dumps(
            {
                "telemetry": {
                    "anonymous_id": "11111111-1111-4111-8111-111111111111",
                    "enabled": enabled,
                    "consent_version": consent_version,
                    "consented_at": "2026-04-30T00:00:00Z",
                }
            }
        )
    )


def test_non_tty_does_not_fire_prompt(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, False)
    # Make input() error out so a leak is loud.
    monkeypatch.setattr(
        "builtins.input", lambda _p="": (_ for _ in ()).throw(AssertionError("input() called"))
    )

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section.get("enabled") is None
    assert section.get("consent_version") is None


def test_redirected_stdout_does_not_fire_prompt(nauro_home, monkeypatch):
    """Regression: post-commit hook backgrounds `nauro extract > /dev/null 2>&1 &`.

    Stdin is still the user's terminal (isatty=True) but stdout is redirected.
    Without the stdout TTY check the prompt would call input() and SIGTTIN-suspend
    the background job — violating the hook's "must never block" contract.
    """
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda _p="": (_ for _ in ()).throw(AssertionError("input() called"))
    )

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section.get("enabled") is None
    assert section.get("consent_version") is None


def test_env_var_zero_does_not_fire_prompt(nauro_home, monkeypatch):
    monkeypatch.setenv("NAURO_TELEMETRY", "0")
    _set_tty(monkeypatch, True)
    monkeypatch.setattr(
        "builtins.input", lambda _p="": (_ for _ in ()).throw(AssertionError("input() called"))
    )

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    # Config may or may not have been touched by get_telemetry_config (it generates anonymous_id),
    # but consent fields must remain unset.
    section = _read_telemetry(nauro_home)
    assert section.get("enabled") is None
    assert section.get("consent_version") is None


def test_tty_user_enters_y(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "y")

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is True
    assert section["consent_version"] == 1
    parsed = datetime.fromisoformat(section["consented_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_tty_user_enters_n(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "n")

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is False
    assert section["consent_version"] == 1


def test_first_run_empty_input_defaults_to_yes(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "")

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is True
    assert section["consent_version"] == 1


def test_consent_version_bump_re_triggers_prompt(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed(nauro_home, enabled=True, consent_version=1)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "n")

    import nauro.telemetry.consent as consent_mod

    monkeypatch.setattr(consent_mod, "TELEMETRY_CONSENT_VERSION", 2)

    consent_mod.maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["consent_version"] == 2
    assert section["enabled"] is False


def test_reprompt_default_preserves_previous_yes(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed(nauro_home, enabled=True, consent_version=1)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "")

    import nauro.telemetry.consent as consent_mod

    monkeypatch.setattr(consent_mod, "TELEMETRY_CONSENT_VERSION", 2)

    consent_mod.maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["consent_version"] == 2
    assert section["enabled"] is True


def test_reprompt_default_preserves_previous_no(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed(nauro_home, enabled=False, consent_version=1)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "")

    import nauro.telemetry.consent as consent_mod

    monkeypatch.setattr(consent_mod, "TELEMETRY_CONSENT_VERSION", 2)

    consent_mod.maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["consent_version"] == 2
    assert section["enabled"] is False


def test_already_consented_at_current_version_does_not_re_prompt(nauro_home, monkeypatch):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed(nauro_home, enabled=True, consent_version=1)
    _set_tty(monkeypatch, True)
    monkeypatch.setattr(
        "builtins.input", lambda _p="": (_ for _ in ()).throw(AssertionError("input() called"))
    )

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    # Untouched: still the seeded values.
    assert section["enabled"] is True
    assert section["consent_version"] == 1
    assert section["consented_at"] == "2026-04-30T00:00:00Z"
