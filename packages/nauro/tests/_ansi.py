"""Shared ANSI-stripping helper for CLI output assertions.

Typer renders --help and BadParameter messages through Rich, which wraps
flag tokens in bold/colour escapes when the runner detects a colour-capable
terminal (CI runners with ``FORCE_COLOR=1``, GH Actions, etc.). The escapes
split substrings like ``--rejected`` into ``-\\x1b[0m\\x1b[1m-rejected``,
breaking literal ``"--rejected" in output`` checks. ``NO_COLOR`` only
suppresses colour, not bold/dim — stripping here is the only
environment-independent fix.
"""

from __future__ import annotations


def strip_ansi(text: str) -> str:
    """Strip ANSI CSI escape sequences using plain string ops (no regex)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\x1b" and i + 1 < n and text[i + 1] == "[":
            i += 2
            while i < n and text[i] != "m":
                i += 1
            i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)
