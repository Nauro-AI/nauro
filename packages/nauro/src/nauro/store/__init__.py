"""Project store package — all store operations go through this module."""

from nauro.store.filesystem_store import FilesystemStore as FilesystemStore

__all__ = ["FilesystemStore"]
