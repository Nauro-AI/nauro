"""Gate ratcheted ruff rules against a shrink-only baseline.

The rules in RATCHETED_RULES are ignored by the main ruff check and re-selected
here over SOURCES under the root ruff config, so its per-file exemptions for
tests still apply. Findings are keyed by path and rule, ignoring noqa comments,
and compared with scripts/ruff_budget_baseline.jsonl: a new key, a count above
the baseline, and a count below the baseline all fail, so recorded debt can only
go down and must be re-recorded when it does.

Usage: check_ruff_budget.py [--baseline PATH] [--write-baseline]
Exits 0 when the baseline matches, 1 on drift, 2 when the analyzer or the
baseline is unavailable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

RATCHETED_RULES: tuple[str, ...] = (
    "B008",
    "B904",
    "B905",
    "BLE001",
    "C901",
    "E501",
    "N818",
    "PERF401",
    "PERF403",
    "PLR0911",
    "PLR0912",
    "PLR0915",
    "PLW0603",
    "PLW2901",
    "RET501",
    "RET504",
    "RET505",
    "S110",
    "S112",
    "SIM102",
    "SIM103",
    "SIM105",
    "SIM108",
    "SIM117",
    "SIM300",
    "SIM905",
    "TRY004",
    "TRY201",
    "TRY300",
    "TRY301",
)
SOURCES: tuple[str, ...] = ("packages", "benchmarks", "scripts")
RUFF_VERSION = "ruff 0.15.20"
DEFAULT_BASELINE = Path("scripts/ruff_budget_baseline.jsonl")

Key = tuple[str, str]


def ratcheted_rule(value: str) -> str:
    """Accept a rule code only when this gate ratchets it."""
    if value not in RATCHETED_RULES:
        raise ValueError(f"{value} is not a ratcheted rule")
    return value


class BaselineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    rule: str
    count: int = Field(gt=0)

    _rule = field_validator("rule")(ratcheted_rule)

    @field_validator("path")
    @classmethod
    def relative_source(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError("baseline paths must be canonical relative paths")
        if not value.endswith(".py") or "\\" in value:
            raise ValueError("baseline paths must name Python source files")
        return value

    @property
    def key(self) -> Key:
        return (self.path, self.rule)


class RuffDiagnostic(BaseModel):
    model_config = ConfigDict(strict=True)

    filename: str
    code: str

    _code = field_validator("code")(ratcheted_rule)


def read_baseline(path: Path) -> Counter[Key]:
    counts: Counter[Key] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = BaselineEntry.model_validate_json(line)
        if entry.key in counts:
            raise ValueError("duplicate baseline entry")
        counts[entry.key] = entry.count
    return counts


def write_baseline(path: Path, counts: Counter[Key]) -> None:
    entries = (
        BaselineEntry(path=file, rule=rule, count=count)
        for (file, rule), count in sorted(counts.items())
    )
    body = "".join(entry.model_dump_json() + "\n" for entry in entries)
    path.write_text(body, encoding="utf-8")


def _check_ruff_version() -> None:
    version = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    if version.stdout.strip() != RUFF_VERSION:
        raise ValueError(f"expected {RUFF_VERSION}")


def _run_ruff(root: Path, sources: list[str]) -> list[object]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--ignore-noqa",
            "--select",
            ",".join(RATCHETED_RULES),
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
    return raw


def collect(root: Path, sources: list[str]) -> Counter[Key]:
    """Count current findings per path and rule, noqa comments included."""
    _check_ruff_version()
    counts: Counter[Key] = Counter()
    for item in _run_ruff(root, sources):
        diagnostic = RuffDiagnostic.model_validate(item)
        path = Path(diagnostic.filename).relative_to(root).as_posix()
        entry = BaselineEntry(path=path, rule=diagnostic.code, count=1)
        counts[entry.key] += 1
    return counts


def differences(expected: Counter[Key], observed: Counter[Key]) -> list[str]:
    return [
        f"{path} {rule}: expected {expected[path, rule]}, observed {observed[path, rule]}"
        for path, rule in sorted(expected.keys() | observed.keys())
        if expected[path, rule] != observed[path, rule]
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--write-baseline", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    baseline = args.baseline if args.baseline.is_absolute() else root / args.baseline
    try:
        observed = collect(root, list(SOURCES))
        if args.write_baseline:
            write_baseline(baseline, observed)
            print(f"Wrote {len(observed)} entries to {args.baseline}")
            return 0
        expected = read_baseline(baseline)
    except (OSError, ValueError, ValidationError, subprocess.SubprocessError) as exc:
        print(f"Quality check unavailable: {exc}", file=sys.stderr)
        return 2
    changes = differences(expected, observed)
    if changes:
        print("\n".join(changes))
        print("Review new findings or lower stale baseline entries.")
        return 1
    print(f"Ruff budget baseline matches: {sum(observed.values())} findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
