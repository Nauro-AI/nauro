"""Failure checks for the ruff budget gate."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from check_ruff_budget import (
    RATCHETED_RULES,
    RUFF_VERSION,
    SOURCES,
    collect,
    differences,
    main,
    read_baseline,
    write_baseline,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PERMANENT_IGNORES = {"TRY003", "PLR0913"}
TEST_EXEMPTIONS = {"packages/*/tests/**": ["PLR0915", "C901", "BLE001"]}
ROOT = Path(__file__).resolve().parents[1]


class BudgetTests(unittest.TestCase):
    def test_real_analyzer_bypasses_config_ignore_and_noqa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text(
                "try:\n    pass\nexcept Exception:  # noqa: BLE001, S110\n    pass\n",
                encoding="utf-8",
            )
            (root / "ruff.toml").write_text(
                '[lint]\nignore = ["BLE001", "S110"]\n', encoding="utf-8"
            )
            self.assertEqual(
                collect(root, ["src"]),
                Counter(
                    {
                        ("src/a.py", "BLE001"): 1,
                        ("src/a.py", "S110"): 1,
                        ("src/a.py", "SIM105"): 1,
                    }
                ),
            )

    def test_ratcheted_rules_are_ignored_by_the_main_check(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lint = config["tool"]["ruff"]["lint"]
        self.assertEqual(set(lint["ignore"]), PERMANENT_IGNORES | set(RATCHETED_RULES))
        self.assertEqual(lint["per-file-ignores"], TEST_EXEMPTIONS)
        self.assertEqual(list(RATCHETED_RULES), sorted(set(RATCHETED_RULES)))
        for source in SOURCES:
            self.assertTrue((ROOT / source).is_dir(), source)

    def test_missing_baseline_is_an_error(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(FileNotFoundError),
        ):
            read_baseline(Path(directory) / "missing.jsonl")

    def test_baseline_rejects_invalid_and_duplicate_rows(self):
        valid = '{"path":"src/a.py","rule":"BLE001","count":1}'
        rows = [
            "{",
            valid + "\n" + valid,
            valid.replace("src/a.py", "../a.py"),
            valid.replace("src/a.py", "/a.py"),
            valid.replace("src/a.py", "src//a.py"),
            valid.replace("BLE001", "UNKNOWN"),
            valid.replace("BLE001", "TRY003"),
            valid.replace('"count":1', '"count":0'),
            valid.replace('"count":1', '"count":true'),
            valid.replace('"count":1', '"count":"1"'),
            valid.replace('"count":1', '"count":1,"extra":1'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.jsonl"
            for row in rows:
                with self.subTest(row=row):
                    path.write_text(row, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_baseline(path)
            path.write_text(valid, encoding="utf-8")
            self.assertEqual(read_baseline(path), Counter({("src/a.py", "BLE001"): 1}))

    def test_written_baseline_is_sorted_and_reads_back(self):
        counts = Counter({("src/b.py", "C901"): 2, ("src/a.py", "S110"): 1})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.jsonl"
            write_baseline(path, counts)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                lines,
                [
                    '{"path":"src/a.py","rule":"S110","count":1}',
                    '{"path":"src/b.py","rule":"C901","count":2}',
                ],
            )
            self.assertEqual(read_baseline(path), counts)

    def test_new_increased_and_reduced_findings_are_visible(self):
        expected = Counter({("src/a.py", "BLE001"): 2})
        cases = [
            (Counter(), ["src/a.py BLE001: expected 2, observed 0"]),
            (Counter({("src/a.py", "BLE001"): 3}), ["src/a.py BLE001: expected 2, observed 3"]),
            (Counter({("src/a.py", "BLE001"): 1}), ["src/a.py BLE001: expected 2, observed 1"]),
            (
                Counter({("src/b.py", "BLE001"): 2}),
                [
                    "src/a.py BLE001: expected 2, observed 0",
                    "src/b.py BLE001: expected 0, observed 2",
                ],
            ),
        ]
        for observed, messages in cases:
            with self.subTest(observed=observed):
                self.assertEqual(differences(expected, observed), messages)
        self.assertEqual(differences(expected, expected), [])

    def test_analyzer_failures_do_not_become_empty_success(self):
        version = subprocess.CompletedProcess([], 0, RUFF_VERSION + "\n", "")
        reports = [
            (2, "[]", ""),
            (1, "[]", ""),
            (0, "{}", ""),
            (0, "[]", "warning"),
            (0, "{", ""),
            (1, '[{"filename":"/src/a.py","code":"UNKNOWN"}]', ""),
        ]
        for code, body, error in reports:
            with self.subTest(code=code, body=body, error=error):
                report = subprocess.CompletedProcess([], code, body, error)
                with (
                    patch("check_ruff_budget.subprocess.run", side_effect=[version, report]),
                    self.assertRaises(ValueError),
                ):
                    collect(Path.cwd(), ["src"])

    def test_analyzer_version_and_suppression_bypass(self):
        root = Path.cwd()
        version = subprocess.CompletedProcess([], 0, RUFF_VERSION + "\n", "")
        body = json.dumps([{"filename": str(root / "src/a.py"), "code": "BLE001"}])
        report = subprocess.CompletedProcess([], 1, body, "")
        with patch("check_ruff_budget.subprocess.run", side_effect=[version, report]) as run:
            self.assertEqual(collect(root, ["src"]), Counter({("src/a.py", "BLE001"): 1}))
        self.assertIn("--ignore-noqa", run.call_args.args[0])
        self.assertIn(",".join(RATCHETED_RULES), run.call_args.args[0])
        version = subprocess.CompletedProcess([], 0, "ruff 0.15.8\n", "")
        with (
            patch("check_ruff_budget.subprocess.run", return_value=version),
            self.assertRaisesRegex(ValueError, "expected ruff"),
        ):
            collect(root, ["src"])

    def test_exit_codes_distinguish_unavailable_drift_and_success(self):
        with patch("sys.argv", ["check_ruff_budget.py"]), patch("builtins.print"):
            with patch("check_ruff_budget.collect", side_effect=ValueError("invalid")):
                self.assertEqual(main(), 2)
            with patch("check_ruff_budget.collect", return_value=Counter()):
                with patch("check_ruff_budget.read_baseline", side_effect=OSError("missing")):
                    self.assertEqual(main(), 2)
                with patch("check_ruff_budget.read_baseline", return_value=Counter()):
                    self.assertEqual(main(), 0)
                with patch(
                    "check_ruff_budget.read_baseline",
                    return_value=Counter({("a.py", "C901"): 1}),
                ):
                    self.assertEqual(main(), 1)

    def test_write_baseline_flag_records_current_findings(self):
        counts = Counter({("src/a.py", "BLE001"): 1})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.jsonl"
            argv = ["check_ruff_budget.py", "--baseline", str(path), "--write-baseline"]
            with (
                patch("sys.argv", argv),
                patch("builtins.print"),
                patch("check_ruff_budget.collect", return_value=counts),
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(read_baseline(path), counts)


if __name__ == "__main__":
    unittest.main()
