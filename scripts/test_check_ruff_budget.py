"""Failure checks for the source-risk baseline gate."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from check_ruff_budget import RUFF_VERSION, collect, differences, main, read_baseline


class BudgetTests(unittest.TestCase):
    def test_real_analyzer_counts_suppressed_source_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text(
                "try:\n    pass\nexcept Exception:  # noqa: BLE001, S110\n    pass\n",
                encoding="utf-8",
            )
            (root / "ruff.toml").write_text('exclude = ["src"]\n', encoding="utf-8")
            self.assertEqual(
                collect(root, ["src"]),
                Counter({("src/a.py", "BLE001"): 1, ("src/a.py", "S110"): 1}),
            )

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

    def test_new_increased_and_reduced_findings_are_visible(self):
        expected = Counter({("src/a.py", "BLE001"): 2})
        cases = [
            (Counter(), ["src/a.py BLE001: expected 2, observed 0"]),
            (Counter({("src/a.py", "BLE001"): 3}), ["src/a.py BLE001: expected 2, observed 3"]),
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
        self.assertEqual(
            differences(expected, Counter()),
            ["src/a.py BLE001: expected 2, observed 0"],
        )

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
        version = subprocess.CompletedProcess([], 0, "ruff 0.15.8\n", "")
        with (
            patch("check_ruff_budget.subprocess.run", return_value=version),
            self.assertRaisesRegex(ValueError, "expected ruff"),
        ):
            collect(root, ["src"])

    def test_exit_codes_distinguish_unavailable_drift_and_success(self):
        with patch("sys.argv", ["check_ruff_budget.py", "src"]), patch("builtins.print"):
            with patch("check_ruff_budget.read_baseline", side_effect=OSError("missing")):
                self.assertEqual(main(), 2)
            with patch("check_ruff_budget.read_baseline", return_value=Counter()):
                with patch("check_ruff_budget.collect", side_effect=ValueError("invalid")):
                    self.assertEqual(main(), 2)
                with patch(
                    "check_ruff_budget.collect", return_value=Counter({("a.py", "C901"): 1})
                ):
                    self.assertEqual(main(), 1)
                with patch("check_ruff_budget.collect", return_value=Counter()):
                    self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
