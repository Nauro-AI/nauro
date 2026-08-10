"""The declared nauro-core floor must cover the names nauro imports from it.

nauro and nauro-core release independently: nauro can import a constant that
only exists in an unreleased core, and nothing in the workspace notices,
because the workspace resolves core from source. The published wheel then
installs against an older core and fails at import.

The coupling is NOT automatic. ``NEWEST_IMPORTED_CONSTANT`` and
``INTRODUCED_IN`` below are hand-maintained pins: this test proves the
declared floor covers the one name it has been told about, and nothing more.
It cannot discover a newly imported constant on its own, so adding an import
from a later core release means updating both pins in the same change.
"""

from __future__ import annotations

from pathlib import Path

import nauro_core
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

CORE_DISTRIBUTION = "nauro-core"

# Hand-maintained pins, updated together whenever nauro starts importing a
# name from a later nauro-core release: the newest nauro-core public constant
# nauro imports, and the core release that first exported it.
NEWEST_IMPORTED_CONSTANT = "OPEN_QUESTIONS_DEFAULT_BODY"
INTRODUCED_IN = Version("1.11.0")


def _core_requirement() -> Requirement:
    """Return the parsed nauro-core entry from nauro's declared dependencies."""
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    matches = [
        requirement
        for requirement in map(Requirement, manifest["project"]["dependencies"])
        if canonicalize_name(requirement.name) == canonicalize_name(CORE_DISTRIBUTION)
    ]
    assert len(matches) == 1, (
        f"expected exactly one {CORE_DISTRIBUTION} dependency in {PYPROJECT}, "
        f"found {[str(match) for match in matches]}"
    )
    return matches[0]


def _declared_floor(specifier: SpecifierSet) -> Version:
    """Return the lower bound *specifier* pins nauro-core to."""
    lower_bounds = [
        Version(clause.version) for clause in specifier if clause.operator in {">=", "==", "~="}
    ]
    assert lower_bounds, f"{CORE_DISTRIBUTION} is declared without a lower bound: {specifier}"
    return max(lower_bounds)


def test_declared_core_floor_covers_newest_imported_constant() -> None:
    assert _declared_floor(_core_requirement().specifier) >= INTRODUCED_IN


def test_newest_imported_constant_is_in_the_core_public_api() -> None:
    assert NEWEST_IMPORTED_CONSTANT in nauro_core.__all__
