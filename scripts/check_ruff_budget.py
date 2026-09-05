"""Reject new source-risk diagnostics and stale baseline counts."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Rule = Literal["BLE001", "C901", "S110", "S112"]
RULES = "BLE001,C901,S110,S112"
RUFF_VERSION = "ruff 0.15.20"


class BaselineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    rule: Rule
    count: int = Field(gt=0)

    @field_validator("path")
    @classmethod
    def relative_source(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError("baseline paths must be canonical relative paths")
        if not value.endswith(".py") or "\\" in value:
            raise ValueError("baseline paths must name Python source files")
        return value


class RuffDiagnostic(BaseModel):
    model_config = ConfigDict(strict=True)

    filename: str
    code: Rule


def read_baseline(path: Path) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = BaselineEntry.model_validate_json(line)
        key = (entry.path, entry.rule)
        if key in counts:
            raise ValueError("duplicate baseline entry")
        counts[key] = entry.count
    return counts


def collect(root: Path, sources: list[str]) -> Counter[tuple[str, str]]:
    version = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    if version.stdout.strip() != RUFF_VERSION:
        raise ValueError(f"expected {RUFF_VERSION}")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--ignore-noqa",
            "--select",
            RULES,
            "--config",
            "lint.mccabe.max-complexity=12",
            "--output-format",
            "json",
            *sources,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise ValueError("ruff failed to produce a complete diagnostic report")
    raw = json.loads(result.stdout)
    if not isinstance(raw, list) or (result.returncode == 0) != (len(raw) == 0):
        raise ValueError("ruff returned an inconsistent diagnostic report")
    counts: Counter[tuple[str, str]] = Counter()
    for item in raw:
        diagnostic = RuffDiagnostic.model_validate(item)
        path = Path(diagnostic.filename).relative_to(root).as_posix()
        entry = BaselineEntry(path=path, rule=diagnostic.code, count=1)
        counts[(entry.path, entry.rule)] += 1
    return counts


def differences(
    expected: Counter[tuple[str, str]], observed: Counter[tuple[str, str]]
) -> list[str]:
    return [
        f"{path} {rule}: expected {expected[path, rule]}, observed {observed[path, rule]}"
        for path, rule in sorted(expected.keys() | observed.keys())
        if expected[path, rule] != observed[path, rule]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        expected = read_baseline(root / "scripts/ruff_budget_baseline.jsonl")
        observed = collect(root, args.sources)
    except (OSError, ValueError, ValidationError, subprocess.SubprocessError) as exc:
        print(f"Quality check unavailable: {exc}", file=sys.stderr)
        return 2
    changes = differences(expected, observed)
    if changes:
        print("\n".join(changes))
        print("Review new findings or remove stale baseline entries.")
        return 1
    print(f"Source-risk baseline matches: {sum(observed.values())} findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
