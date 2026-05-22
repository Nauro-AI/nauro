"""Cross-transport operations kernel.

Pure callables that implement the user-facing operations exposed by every
Nauro transport (CLI, local stdio MCP, remote HTTP MCP). Each operation
takes a ``Store`` and returns a typed Pydantic ``Result`` model. Operations
emit no telemetry; transports own event emission.
"""

from nauro_core.operations._in_memory_store import InMemoryStore as InMemoryStore
from nauro_core.operations.check_decision import check_decision as check_decision
from nauro_core.operations.get_decision import get_decision as get_decision
from nauro_core.operations.get_raw_file import get_raw_file as get_raw_file
from nauro_core.operations.list_decisions import list_decisions as list_decisions
from nauro_core.operations.results import CheckDecisionResult as CheckDecisionResult
from nauro_core.operations.results import DecisionSummary as DecisionSummary
from nauro_core.operations.results import GetDecisionResult as GetDecisionResult
from nauro_core.operations.results import GetRawFileResult as GetRawFileResult
from nauro_core.operations.results import ListDecisionsResult as ListDecisionsResult
from nauro_core.operations.store import Store as Store

from . import results as results

__all__ = [
    "CheckDecisionResult",
    "DecisionSummary",
    "GetDecisionResult",
    "GetRawFileResult",
    "InMemoryStore",
    "ListDecisionsResult",
    "Store",
    "check_decision",
    "get_decision",
    "get_raw_file",
    "list_decisions",
    "results",
]
