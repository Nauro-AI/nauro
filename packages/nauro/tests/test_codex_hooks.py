"""Contract tests for Codex hook configuration handling."""

import json

import pytest

from nauro.cli._codex_hooks import (
    _CODEX_HOOK_EVENTS,
    _CodexHookConfigError,
    _format_codex_hooks,
    _inspect_codex_hooks,
    _inspect_nauro_hook,
    _parse_codex_hooks,
    _render_nauro_hook,
    _transform_codex_hooks,
)


def test_codex_hook_codec_preserves_canonical_format():
    config = _parse_codex_hooks('{"theme":"démarrage"}')

    assert config == {"theme": "démarrage"}
    assert _format_codex_hooks(config) == '{\n  "theme": "d\\u00e9marrage"\n}\n'


def test_codex_hook_parser_rejects_non_object_root():
    with pytest.raises(_CodexHookConfigError) as exc_info:
        _parse_codex_hooks("[]")
    assert str(exc_info.value) == ".codex/hooks.json is not a JSON object, skipped"


def test_rendered_hook_round_trips_on_posix_and_windows():
    command = "/opt/Nauro's Tools/A &foo/nauro"
    entry = _render_nauro_hook(command)

    posix = _inspect_nauro_hook(entry, windows=False)
    windows = _inspect_nauro_hook(entry, windows=True)

    assert posix is not None
    assert posix.recorded_command == command
    assert windows is not None
    assert windows.recorded_command == command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            '"C:\\Program Files\\Nauro\\nauro.exe" hook codex-bootstrap',
            "C:\\Program Files\\Nauro\\nauro.exe",
        ),
        ("nauro.exe hook codex-bootstrap", "nauro.exe"),
        (
            "C:\\O'Brien\\nauro.exe hook codex-bootstrap",
            "C:\\O'Brien\\nauro.exe",
        ),
        (
            r"C:\Nauro;Tools\{current}\nauro.exe hook codex-bootstrap",
            r"C:\Nauro;Tools\{current}\nauro.exe",
        ),
        ("powershell.exe -Command \"& 'nauro' hook codex-bootstrap\"", "nauro"),
    ],
)
def test_inspector_accepts_supported_windows_invocations(command: str, expected: str):
    parsed = _inspect_nauro_hook(
        {"command": "user-posix-hook", "commandWindows": command},
        windows=True,
    )

    assert parsed is not None
    assert parsed.recorded_command == expected


def test_empty_windows_override_disables_posix_fallback():
    entry = {
        "command": "nauro hook codex-bootstrap",
        "commandWindows": "",
    }

    assert _inspect_nauro_hook(entry, windows=True) is None


def test_marked_but_unparseable_hook_remains_present():
    entry = {
        "command": "user-posix-hook",
        "commandWindows": 'powershell.exe -Command "nauro hook codex-bootstrap"',
    }
    parsed = _inspect_nauro_hook(entry, windows=True)

    assert parsed is not None
    assert parsed.recorded_command is None


def test_hook_state_tracks_each_event_and_recorded_command():
    entry = _render_nauro_hook("/opt/nauro")
    config = {"hooks": {event: [{"hooks": [entry]}] for event in _CODEX_HOOK_EVENTS}}

    state = _inspect_codex_hooks(config, windows=False)

    assert state.present is True
    assert state.complete is True
    assert state.recorded_commands == ("/opt/nauro", "/opt/nauro")


def test_transform_replaces_nauro_hooks_without_mutating_input():
    original = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "load-notes"},
                        {
                            "type": "command",
                            "command": "old-nauro hook codex-bootstrap",
                        },
                    ],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "cleanup"}]}],
        },
        "theme": "dark",
    }

    transformed = _transform_codex_hooks(original, command="/new/nauro")

    assert original["hooks"]["SessionStart"][0]["hooks"][1]["command"].startswith("old-nauro")
    assert transformed.removed == 1
    assert transformed.config["theme"] == "dark"
    assert transformed.config["hooks"]["Stop"] == original["hooks"]["Stop"]
    state = _inspect_codex_hooks(transformed.config, windows=False)
    assert state.complete is True
    assert state.recorded_commands == ("/new/nauro", "/new/nauro")


def test_remove_preserves_user_matcher_metadata_and_empty_matcher():
    nauro = _render_nauro_hook("nauro")
    config = {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "custom": "keep", "hooks": [nauro]},
                {"matcher": "manual", "hooks": []},
            ],
            "SubagentStart": [{"hooks": [nauro]}],
        }
    }

    transformed = _transform_codex_hooks(config, command=None)

    assert transformed.removed == 2
    assert transformed.config == {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "custom": "keep", "hooks": []},
                {"matcher": "manual", "hooks": []},
            ]
        }
    }


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {"hooks": []},
            "hooks key in .codex/hooks.json is not a JSON object, skipped",
        ),
        (
            {"hooks": {"SessionStart": {}}},
            "hooks.SessionStart is not a JSON array, skipped",
        ),
    ],
)
def test_transform_rejects_unsafe_shapes(config: dict, message: str):
    with pytest.raises(_CodexHookConfigError) as exc_info:
        _transform_codex_hooks(config, command="nauro")
    assert str(exc_info.value) == message


def test_codec_rejects_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        _parse_codex_hooks("{not-json")
