"""Cross-transport operations kernel.

Pure callables that implement the user-facing operations exposed by every
Nauro transport (CLI, local stdio MCP, remote HTTP MCP). Each operation
takes a ``Store`` and returns a typed Pydantic ``Result`` model. Operations
emit no telemetry; transports own event emission.
"""

from nauro_core.operations._in_memory_store import InMemoryStore as InMemoryStore
from nauro_core.operations.check_decision import check_decision as check_decision
from nauro_core.operations.results import CheckDecisionResult as CheckDecisionResult
from nauro_core.operations.store import Store as Store

from . import results as results

__all__ = [
    "CheckDecisionResult",
    "InMemoryStore",
    "Store",
    "check_decision",
    "results",
]
