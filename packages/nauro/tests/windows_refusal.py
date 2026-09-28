"""Platform refusals as Windows and POSIX report them, for the typed-refusal tests."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from nauro.store import _platform_durability as durability

REFUSALS = [
    pytest.param(True, 5, "access is denied or the file is read-only", id="windows-read-only"),
    pytest.param(True, 32, "another process has it open", id="windows-in-use"),
    pytest.param(False, None, os.strerror(errno.EACCES), id="posix-eacces"),
]


def refusing(monkeypatch, windows, winerror, calls):
    """Refuse like the platform, switching to the Windows branch only as the error is raised."""

    def refuse(path, *args, **kwargs):
        calls.append(path)
        monkeypatch.setattr(durability, "WINDOWS", windows)
        error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        if winerror is not None:
            error.winerror = winerror
        raise error

    return refuse


def chmod_calls(monkeypatch):
    """Record every mode or attribute change; a refusal must never clear attributes."""
    calls, chmod = [], os.chmod

    def recorded(path, *args, **kwargs):
        calls.append(Path(path))
        chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", recorded)
    return calls
