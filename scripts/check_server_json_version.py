#!/usr/bin/env python3
"""Guard that ``server.json`` stays version-locked to the ``nauro`` package.

The MCP registry publish job (``publish-nauro.yml``) verifies ``server.json``'s
version against the release tag — but only AFTER the tag is cut. So a release
that bumps ``packages/nauro/pyproject.toml`` without bumping ``server.json``
publishes to PyPI cleanly and then fails the ``publish-registry`` job
post-tag (this happened on the 1.0.1 release). This check moves the guard to
PR time: it fails if ``server.json``'s top-level ``version`` or its first
package's ``version`` drifts from the ``nauro`` package version in
``packages/nauro/pyproject.toml``.

It also enforces the MCP registry's ``description`` length cap (100
characters) — the registry rejects longer descriptions with a 422 at the
same post-tag moment (this happened on the 1.3.0 release, after a copy
rewrite grew the description to 140 characters).

Usage::

    python scripts/check_server_json_version.py
    python scripts/check_server_json_version.py server.json packages/nauro/pyproject.toml

Exits 0 when in lockstep, 1 on a version mismatch or an over-long
description, 2 on a usage/IO error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The MCP registry rejects a server description longer than this (HTTP 422).
MAX_DESCRIPTION_CHARS = 100


def project_version(pyproject_text: str) -> str:
    """Return ``[project].version`` from pyproject text.

    Stdlib-only line parser (no ``tomllib``) so the guard runs on the whole
    ``requires-python >= 3.10`` range, including 3.10 where ``tomllib`` is
    absent. Reads the ``version`` key only within the ``[project]`` table, so a
    ``version`` under another table (e.g. ``[build-system]``) is not matched.
    """
    in_project = False
    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project and stripped.startswith("version") and "=" in stripped:
            return stripped.partition("=")[2].strip().strip("\"'")
    raise KeyError("no [project].version found in pyproject.toml")


def main(argv: list[str]) -> int:
    if len(argv) > 3:
        print(
            "usage: check_server_json_version.py [server.json] [pyproject.toml]",
            file=sys.stderr,
        )
        return 2
    server_json = Path(argv[1]) if len(argv) > 1 else Path("server.json")
    pyproject = Path(argv[2]) if len(argv) > 2 else Path("packages/nauro/pyproject.toml")

    for p in (server_json, pyproject):
        if not p.exists():
            print(f"error: path does not exist: {p}", file=sys.stderr)
            return 2

    try:
        pkg_version = project_version(pyproject.read_text())
        server = json.loads(server_json.read_text())
    except (KeyError, ValueError, OSError) as exc:
        print(f"error: could not read versions: {exc}", file=sys.stderr)
        return 2

    server_version = server.get("version")
    packages = server.get("packages") or [{}]
    package_version = packages[0].get("version")

    mismatches: list[str] = []
    if server_version != pkg_version:
        mismatches.append(f"server.json .version = {server_version!r}")
    if package_version != pkg_version:
        mismatches.append(f"server.json .packages[0].version = {package_version!r}")

    if mismatches:
        print(
            "server.json is out of lockstep with the nauro package "
            f"(packages/nauro/pyproject.toml version = {pkg_version!r}):"
        )
        for m in mismatches:
            print(f"  {m}")
        print(
            "\nBump server.json's `version` and `packages[0].version` to match "
            "before releasing — the publish-time registry check only catches "
            "this after the tag is cut."
        )
        return 1

    description = server.get("description") or ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        print(
            f"server.json .description is {len(description)} characters; the MCP "
            f"registry caps it at {MAX_DESCRIPTION_CHARS} and rejects the publish "
            "with a 422 after the tag is cut. Shorten it before releasing."
        )
        return 1

    print(
        f"server.json is version-locked to the nauro package ({pkg_version}) "
        f"and its description fits the registry cap "
        f"({len(description)}/{MAX_DESCRIPTION_CHARS} chars)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
