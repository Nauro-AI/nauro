"""Tests for the repo's docstring budget guard in ``scripts/``.

The guard is not an installed module, so it is loaded from its path and driven
through ``main`` with an explicit baseline under ``tmp_path``. Fixtures write
docstrings in the real form, with the closing quotes on their own line.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_docstring_budget.py"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docstring_budget", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _docstring(lines: int, indent: str = "") -> str:
    rest = "".join(f"\n{indent}line {index}" for index in range(2, lines + 1))
    return f'{indent}"""line 1{rest}\n{indent}"""'


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.chdir(tmp_path)
    return guard.main(["check_docstring_budget.py", *args])


def _write_source(tmp_path: Path, text: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "sample.py").write_text(text, encoding="utf-8")
    return src


def _kinds(src: Path) -> list[tuple[str, int, int]]:
    return [(v.kind, v.lines, v.budget) for v in guard.violations_in_tree(src)]


def test_module_docstring_at_budget_passes(tmp_path: Path) -> None:
    src = _write_source(tmp_path, _docstring(15) + "\n")
    assert guard.violations_in_tree(src) == []


def test_module_docstring_over_budget_fails(tmp_path: Path) -> None:
    src = _write_source(tmp_path, _docstring(16) + "\n")
    assert _kinds(src) == [("module", 16, 15)]
    assert guard.violations_in_tree(src)[0].identifier.endswith("sample.py::<module>")


def test_class_docstring_boundary(tmp_path: Path) -> None:
    src = _write_source(tmp_path, f"class Ok:\n{_docstring(5, '    ')}\n")
    assert guard.violations_in_tree(src) == []
    src = _write_source(tmp_path, f"class Bad:\n{_docstring(6, '    ')}\n")
    assert _kinds(src) == [("class", 6, 5)]
    assert guard.violations_in_tree(src)[0].identifier.endswith("sample.py::Bad")


def test_function_docstring_boundary(tmp_path: Path) -> None:
    src = _write_source(tmp_path, f"def ok():\n{_docstring(3, '    ')}\n")
    assert guard.violations_in_tree(src) == []
    src = _write_source(tmp_path, f"def bad():\n{_docstring(4, '    ')}\n")
    assert _kinds(src) == [("function", 4, 3)]
    assert guard.violations_in_tree(src)[0].identifier.endswith("sample.py::bad")


def test_summary_blank_invariant_form_is_three_lines(tmp_path: Path) -> None:
    text = 'def ok():\n    """Summary.\n\n    One invariant.\n    """\n    return 1\n'
    src = _write_source(tmp_path, text)
    assert guard.violations_in_tree(src) == []


def test_interior_blank_lines_still_count(tmp_path: Path) -> None:
    text = 'def bad():\n    """Summary.\n\n    Invariant.\n\n    Extra.\n    """\n'
    src = _write_source(tmp_path, text)
    assert _kinds(src) == [("function", 5, 3)]


def test_missing_docstring_is_fine(tmp_path: Path) -> None:
    src = _write_source(tmp_path, "class Bare:\n    x = 1\n\n\ndef bare():\n    return 1\n")
    assert guard.violations_in_tree(src) == []


def test_method_and_nested_qualnames(tmp_path: Path) -> None:
    text = (
        f"class Holder:\n"
        f"    async def method(self):\n"
        f"{_docstring(4, '        ')}\n"
        f"        def inner():\n"
        f"{_docstring(4, '            ')}\n"
        f"        return inner\n"
    )
    src = _write_source(tmp_path, text)
    names = sorted(v.identifier.split("::")[1] for v in guard.violations_in_tree(src))
    assert names == ["Holder.method", "Holder.method.inner"]


def test_conditional_nesting_keeps_enclosing_qualname(tmp_path: Path) -> None:
    text = f"def outer():\n    if True:\n        def inner():\n{_docstring(4, '            ')}\n"
    src = _write_source(tmp_path, text)
    names = [v.identifier.split("::")[1] for v in guard.violations_in_tree(src)]
    assert names == ["outer.inner"]


def test_baseline_suppresses_listed_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_source(tmp_path, f"def bad():\n{_docstring(4, '    ')}\n")
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("# header\nsrc/sample.py::bad\n", encoding="utf-8")
    assert _run(tmp_path, monkeypatch, "--baseline", str(baseline), "src") == 0


def test_unbaselined_violation_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_source(tmp_path, f"def bad():\n{_docstring(4, '    ')}\n")
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("", encoding="utf-8")
    assert _run(tmp_path, monkeypatch, "--baseline", str(baseline), "src") == 1


def test_stale_baseline_entry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_source(tmp_path, "def fine():\n    return 1\n")
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("src/sample.py::gone\n", encoding="utf-8")
    assert _run(tmp_path, monkeypatch, "--baseline", str(baseline), "src") == 1
    out = capsys.readouterr().out
    assert "src/sample.py::gone" in out
    assert "only shrinks" in out


def test_write_baseline_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_source(tmp_path, f"def bad():\n{_docstring(4, '    ')}\n")
    baseline = tmp_path / "nested" / "baseline.txt"
    args = ("--baseline", str(baseline), "--write-baseline", "src")
    assert _run(tmp_path, monkeypatch, *args) == 0
    assert "src/sample.py::bad" in baseline.read_text(encoding="utf-8").splitlines()
    assert _run(tmp_path, monkeypatch, "--baseline", str(baseline), "src") == 0


def test_missing_scan_path_is_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(tmp_path, monkeypatch, "--baseline", str(tmp_path / "b.txt"), "nope") == 2


def test_no_directory_argument_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, monkeypatch)
    assert excinfo.value.code == 2


def test_repo_baseline_is_current() -> None:
    baseline = guard.read_baseline(REPO_ROOT / "scripts" / "docstring_budget_baseline.txt")
    current = {
        violation.identifier
        for package in ("nauro", "nauro-core")
        for violation in guard.violations_in_tree(REPO_ROOT / "packages" / package / "src")
    }
    assert baseline == current
