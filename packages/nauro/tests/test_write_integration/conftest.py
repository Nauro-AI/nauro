"""Shared fixtures for the write-integration test subdirectory.

The kernel commits propose_decision on Tier 1 clean — there is no
pending-state machine to reset between tests. This module is kept so the
subdirectory has a discoverable conftest if shared fixtures land later.
"""
