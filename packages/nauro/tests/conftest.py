"""Shared pytest configuration.

Applies nauro config (e.g. api_key → ANTHROPIC_API_KEY) to the environment
so integration tests can use credentials configured via `nauro config set`
without requiring explicit env var injection.
"""

from nauro.store.config import apply_config_to_env

apply_config_to_env()
