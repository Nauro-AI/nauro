# Nauro PyPI Pre-Publish Audit

**Date:** 2026-03-31
**Ready to publish:** NO — 3 blocking issues, 7 should-fix

---

## Blocking Issues (install failure or broken CLI)

### B1. No `readme` field in pyproject.toml — PyPI page will be blank
**File:** `nauro/pyproject.toml`
**What:** The `readme` field is missing. Hatchling won't include README.md in the wheel metadata. PyPI renders a blank project page — the #1 signal that screams "don't install this".
**Fix:** Add `readme = "README.md"` under `[project]`.

### B2. No `authors` field — PyPI shows "Unknown Author"
**File:** `nauro/pyproject.toml`
**What:** Missing entirely. PyPI will show "Author: UNKNOWN" which kills trust for a first-time install.
**Fix:** Add `authors = [{name = "Thomas Thomsen", email = "..."}]`

### B3. No `[project.urls]` — no link from PyPI back to repo/docs
**File:** `nauro/pyproject.toml`
**What:** Users can't find the source code, file issues, or verify the project is real. PyPI shows no links.
**Fix:**
```toml
[project.urls]
Homepage = "https://github.com/nauro-ai/nauro"
Repository = "https://github.com/nauro-ai/nauro"
Issues = "https://github.com/nauro-ai/nauro/issues"
```

---

## Should-Fix Before Publish (embarrassing but functional)

### S1. `nauro --version` doesn't work
**Evidence:** `nauro --version` returns "No such option: --version". Users expect this.
**File:** `nauro/src/nauro/cli/main.py`
**Fix:** Add a version callback to the Typer app with a `--version` option.

### S2. `watchdog` is a required dependency but never imported
**Evidence:** `grep -r watchdog src/nauro/` returns zero hits. Dead weight — adds ~2MB to install.
**File:** `nauro/pyproject.toml` line 21
**Fix:** Remove `"watchdog>=3.0"` from `dependencies`.

### S3. `openai` is a required dependency but only lazy-imported in one function
**Evidence:** Only used in `validation/tier2.py:136` inside a function body for optional embedding similarity. A user who never uses validation tier 2 still pays for the 15MB+ openai SDK.
**File:** `nauro/pyproject.toml` line 16
**Fix:** Move `openai>=1.0` to `[project.optional-dependencies]` (e.g., `validation = ["openai>=1.0"]`). Handle ImportError gracefully in tier2.py.

### S4. `anthropic` is required but Nauro should work without it
**Evidence:** Extraction and validation need it, but core CLI commands (init, note, sync, log, diff, status, config) do not. Users without an Anthropic key still must install the SDK.
**File:** `nauro/pyproject.toml` line 15
**Fix:** Move to optional: `extraction = ["anthropic>=0.18"]`. The current lazy-import pattern in extraction already handles this — just need the dependency to be optional.

### S5. No classifiers in pyproject.toml
**File:** `nauro/pyproject.toml`
**What:** Missing `Development Status`, `License`, `Programming Language :: Python :: 3.11`, etc. PyPI search/filtering won't work.
**Fix:** Add:
```toml
classifiers = [
    "Development Status :: 3 - Alpha",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
]
```

### S6. Test files included in sdist (43 of 111 files)
**Evidence:** `tar tzf dist/nauro-0.1.0.tar.gz` shows all `tests/` files bundled. Not in the wheel (good), but bloats the sdist from 102KB to 195KB.
**Fix:** Add to pyproject.toml:
```toml
[tool.hatch.build.targets.sdist]
exclude = ["tests/"]
```

### S7. No `py.typed` marker file
**Evidence:** D66 added mypy config and type annotations across 55 files, but PEP 561 marker is missing — downstream consumers can't use the types.
**Fix:** Create empty `nauro/src/nauro/py.typed` file.

---

## Post-Publish Improvements (nice to have)

- **P1.** `uvicorn` and `fastapi` could be optional (only needed for `nauro serve`)
- **P2.** `boto3` could be optional (only needed for cloud sync)
- **P3.** No CHANGELOG.md
- **P4.** No upper bounds on dependency versions
- **P5.** `setup claude-code --dry-run` not available despite README implying it

---

## Summary

| Category | Count | Details |
|---|---|---|
| **Blocking** | 3 | Missing readme, authors, urls in pyproject.toml |
| **Should-fix** | 7 | No --version, phantom deps (watchdog, openai as required), no classifiers, test files in sdist, no py.typed |
| **Post-publish** | 5 | Optional deps for serve/sync, changelog, version pinning, dry-run |

**PyPI name "nauro":** Available (confirmed).
**Build:** Succeeds, produces 102KB wheel + 195KB sdist.
**Install test:** All core commands work (init, status, config set, --help in 111ms). Only --version fails.
**Import test:** `python -c "import nauro; print(nauro.__version__)"` works, returns 0.1.0.

The 3 blocking issues are all in pyproject.toml and can be fixed in under 5 minutes. The should-fix items are another 30 minutes of work. After that, ready to publish.
