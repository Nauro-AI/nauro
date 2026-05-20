"""Cross-transport operations kernel.

Pure callables that implement the user-facing operations exposed by every
Nauro transport (CLI, local stdio MCP, remote HTTP MCP). Each operation
takes a ``Store`` and returns a typed Pydantic ``Result`` model. Operations
emit no telemetry; transports own event emission.

PR 0 ships the ``Store`` protocol, an ``InMemoryStore`` for tests, and the
``results`` module shell. Concrete operations land per ``Result`` type as
the rollout proceeds.
"""

from nauro_core.operations._in_memory_store import InMemoryStore as InMemoryStore
from nauro_core.operations.store import Store as Store

from . import results as results

__all__ = ["InMemoryStore", "Store", "results"]
