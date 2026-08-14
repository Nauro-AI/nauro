"""Closed membership rules for protected generations and hosted snapshots."""

from __future__ import annotations

from nauro_core.identifiers import IdentifierKind, is_identifier

_TOP_LEVEL_GENERATION_MEMBERS = frozenset(
    {
        "project.md",
        "state.md",
        "state_current.md",
        "state_history.md",
        "stack.md",
        "open-questions.md",
        "questions-provenance.json",
    }
)
_SNAPSHOT_TOP_LEVEL_MEMBERS = _TOP_LEVEL_GENERATION_MEMBERS - {"questions-provenance.json"}


class InvalidGenerationPath(ValueError):
    """Base class for paths refused by protected-generation classification."""


class NoncanonicalGenerationPath(InvalidGenerationPath):
    """A path uses syntax that cannot name a canonical store member."""


class UnknownGenerationPath(InvalidGenerationPath):
    """A canonical path does not belong to the protected generation set."""


def _require_canonical_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise NoncanonicalGenerationPath("generation path must be a non-empty string.")
    if path.startswith("/") or "\\" in path:
        raise NoncanonicalGenerationPath(f"noncanonical generation path: {path!r}.")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise NoncanonicalGenerationPath(f"noncanonical generation path: {path!r}.")
    return path


def _is_decision_member(path: str) -> bool:
    if not path.startswith("decisions/") or not path.endswith(".md"):
        return False
    rest = path[len("decisions/") : -len(".md")]
    return bool(rest) and "/" not in rest


def _is_context_member(path: str) -> bool:
    if not path.startswith("context/") or not path.endswith(".md"):
        return False
    slug = path[len("context/") : -len(".md")]
    return "/" not in slug and is_identifier(IdentifierKind.brief_slug, slug)


def is_protected_generation_member(path: object) -> bool:
    """Report whether *path* is a canonical protected-generation artifact."""
    try:
        canonical = _require_canonical_path(path)
    except NoncanonicalGenerationPath:
        return False
    return (
        canonical in _TOP_LEVEL_GENERATION_MEMBERS
        or _is_decision_member(canonical)
        or _is_context_member(canonical)
    )


def is_hosted_snapshot_content_member(path: object) -> bool:
    """Report whether *path* belongs in a hosted judgment snapshot."""
    try:
        canonical = _require_canonical_path(path)
    except NoncanonicalGenerationPath:
        return False
    return canonical in _SNAPSHOT_TOP_LEVEL_MEMBERS or _is_decision_member(canonical)


def validate_protected_generation_path(path: object) -> str:
    """Return a protected-generation path, rejecting unknown paths closed."""
    canonical = _require_canonical_path(path)
    if not is_protected_generation_member(canonical):
        raise UnknownGenerationPath(f"path is not a protected generation member: {canonical!r}.")
    return canonical
