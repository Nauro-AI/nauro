"""Schema snapshot guard for ``check_decision`` Result models.

The JSON Schema produced by :class:`CheckDecisionResult` and
:class:`RelatedDecision` is the wire contract every transport adapter
relies on. Any unintentional drift in field names, defaults, or required
markers would silently change how downstream consumers (CLI, local MCP,
remote MCP) render hits. This test pins the schema against a checked-in
snapshot so additive or breaking changes show up as a reviewable diff.

To regenerate the snapshot after an intentional schema change:

    uv run python -c "from nauro_core.operations.results import \\
        CheckDecisionResult, RelatedDecision; import json; \\
        print(json.dumps({'CheckDecisionResult': CheckDecisionResult.model_json_schema(), \\
        'RelatedDecision': RelatedDecision.model_json_schema()}, \\
        indent=2, sort_keys=True))" \\
        > packages/nauro/tests/snapshots/check_decision_schema.json
"""

from __future__ import annotations

import json
from pathlib import Path

from nauro_core.operations.results import CheckDecisionResult, RelatedDecision

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "check_decision_schema.json"


def test_check_decision_result_schema_matches_snapshot():
    current = json.dumps(
        {
            "CheckDecisionResult": CheckDecisionResult.model_json_schema(),
            "RelatedDecision": RelatedDecision.model_json_schema(),
        },
        indent=2,
        sort_keys=True,
    )
    snapshot = SNAPSHOT_PATH.read_text().rstrip("\n")
    assert current == snapshot, (
        "CheckDecisionResult/RelatedDecision JSON schema has drifted from the "
        "checked-in snapshot. If the change is intentional, regenerate the "
        "snapshot using the command in this file's module docstring and review "
        "the diff carefully."
    )
