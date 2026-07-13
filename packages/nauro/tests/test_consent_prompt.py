"""Tests for nauro.telemetry.consent.maybe_prompt."""

from __future__ import annotations

import json
from datetime import datetime

from tests.conftest import seed_consented_config


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
    seed_consented_config(home, enabled=enabled, consent_version=consent_version)


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
    """A backgrounded caller (CI, scripted automation) may inherit a TTY stdin
    while redirecting stdout. Without the stdout TTY check the prompt would call
    input() and SIGTTIN-suspend the job. Belt-and-suspenders: NAURO_TELEMETRY=0
    or stdout-not-a-tty both must skip the prompt.
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


def test_first_run_empty_input_defaults_to_no(nauro_home, monkeypatch):
    """Opt-in: a bare Enter on first run leaves telemetry disabled."""
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "")

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is False
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


def test_first_run_unrecognized_input_defaults_to_no(nauro_home, monkeypatch, capsys):
    """Opt-in: an unrecognized keystroke on first run leaves telemetry disabled,
    rather than being read as consent."""
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "??")  # not y/n/yes/no/empty

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is False  # default_yes=False on first run
    assert section["consent_version"] == 1
    out = capsys.readouterr().out
    assert "Telemetry disabled." in out


def test_reprompt_unrecognized_input_preserves_previous_yes(nauro_home, monkeypatch, capsys):
    """A version-bump re-prompt still applies the stated default (the user's prior
    answer): unrecognized input for a previously-opted-in user stays enabled."""
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed(nauro_home, enabled=True, consent_version=1)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "??")

    import nauro.telemetry.consent as consent_mod

    monkeypatch.setattr(consent_mod, "TELEMETRY_CONSENT_VERSION", 2)

    consent_mod.maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is True  # default_yes=True (previous opt-in)
    assert section["consent_version"] == 2
    out = capsys.readouterr().out
    assert "Telemetry enabled." in out


def test_unrecognized_input_applies_stated_default_no(nauro_home, monkeypatch, capsys):
    """When the default is no (previous opt-out), unrecognized input must preserve it."""
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _seed(nauro_home, enabled=False, consent_version=1)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "??")

    import nauro.telemetry.consent as consent_mod

    monkeypatch.setattr(consent_mod, "TELEMETRY_CONSENT_VERSION", 2)

    consent_mod.maybe_prompt()

    section = _read_telemetry(nauro_home)
    assert section["enabled"] is False  # default_yes=False (previous opt-out)
    assert section["consent_version"] == 2
    out = capsys.readouterr().out
    assert "Telemetry disabled." in out


def test_explicit_yes_echoes_enabled(nauro_home, monkeypatch, capsys):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "y")

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    out = capsys.readouterr().out
    assert "Telemetry enabled." in out


def test_explicit_no_echoes_disabled(nauro_home, monkeypatch, capsys):
    monkeypatch.delenv("NAURO_TELEMETRY", raising=False)
    _set_tty(monkeypatch, True)
    _set_input(monkeypatch, "n")

    from nauro.telemetry.consent import maybe_prompt

    maybe_prompt()

    out = capsys.readouterr().out
    assert "Telemetry disabled." in out
