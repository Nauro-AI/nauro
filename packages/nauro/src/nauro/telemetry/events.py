"""Typed event constructors.

Each function returns a property dict — no emission, no side effects. Call
sites pair these with telemetry.capture() so property-key drift cannot
silently fork between events and the locked taxonomy in PRIVACY.md.
"""

from __future__ import annotations

from typing import Any


def cli_command_invoked(
    command: str,
    success: bool,
    duration_bucket: str,
    nauro_version: str,
    os_name: str,
) -> dict[str, Any]:
    return {
        "command": command,
        "success": success,
        "duration_bucket": duration_bucket,
        "nauro_version": nauro_version,
        "os": os_name,
    }


def mcp_tool_called(
    tool_name: str,
    transport: str,
    success: bool,
    duration_bucket: str,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "transport": transport,
        "success": success,
        "duration_bucket": duration_bucket,
    }


def sync_completed(
    snapshot_count: int,
    duration_bucket: str,
    bytes_bucket: str,
) -> dict[str, Any]:
    return {
        "snapshot_count": snapshot_count,
        "duration_bucket": duration_bucket,
        "bytes_bucket": bytes_bucket,
    }


def project_created(schema_version: int) -> dict[str, Any]:
    return {"schema_version": schema_version}
