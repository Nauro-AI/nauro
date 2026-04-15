"""Payload builders for MCP tool responses.

Thin wrappers around store.reader — all logic lives there.
"""

from pathlib import Path

from nauro.store.reader import read_project_context


def build_l0_payload(store_path: Path) -> str:
    """Build L0 payload (~2,000-4,000 tokens)."""
    return read_project_context(store_path, level=0)


def build_l1_payload(store_path: Path) -> str:
    """Build L1 payload (~4,000-6,000 tokens)."""
    return read_project_context(store_path, level=1)


def build_l2_payload(store_path: Path) -> str:
    """Build L2 payload (full content)."""
    return read_project_context(store_path, level=2)
