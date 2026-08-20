"""Importing nauro_core must not pull the BM25 search stack.

bm25s, Stemmer, and numpy load on the first search call only; a consumer
that never searches never pays their import.
"""

import json
import subprocess
import sys

from conftest import make_decision

_SEARCH_STACK = ("bm25s", "Stemmer", "numpy")


def test_package_import_defers_search_stack():
    probe = (
        "import json, sys\n"
        "import nauro_core\n"
        "import nauro_core.constants\n"
        "import nauro_core.operations\n"
        "import nauro_core.search\n"
        "import nauro_core.validation\n"
        f"print(json.dumps({{m: m in sys.modules for m in {_SEARCH_STACK!r}}}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {m: False for m in _SEARCH_STACK}


def test_first_search_call_loads_stack_and_ranks():
    from nauro_core.search import bm25_search

    decisions = [
        make_decision(1, "Use Auth0 for authentication", "Auth0 handles JWT validation."),
        make_decision(2, "Chose Memcached for session state", "Simpler than Redis here."),
    ]
    results = bm25_search(decisions, "Auth0 JWT")
    assert [r["number"] for r in results] == [1]
    assert all(m in sys.modules for m in _SEARCH_STACK)


def test_tier2_stopwords_resolves_lazily_from_both_surfaces():
    import nauro_core
    import nauro_core.validation

    assert "use" in nauro_core.TIER2_STOPWORDS
    assert nauro_core.validation.TIER2_STOPWORDS == nauro_core.TIER2_STOPWORDS
