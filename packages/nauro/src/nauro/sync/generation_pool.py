"""Lifetime-scoped connection pools with operation-owned HTTP clients."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock

import httpx

from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_credentials import GenerationConnection

_Key = tuple[ResolvedProjectBinding, str, str]


@dataclass
class _Entry:
    transport: httpx.BaseTransport
    borrowers: int = 0


class _BorrowedTransport(httpx.BaseTransport):
    def __init__(self, pool: _Pool, entry: _Entry) -> None:
        self._pool = pool
        self._entry = entry
        self._closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._entry.transport.handle_request(request)

    def close(self) -> None:
        with self._pool.lock:
            if not self._closed:
                self._closed = True
                self._entry.borrowers -= 1
                if self._pool.closed and self._entry.borrowers == 0:
                    self._entry.transport.close()


class _Pool:
    def __init__(self) -> None:
        self.lock = Lock()
        self.closed = False
        self._entries: dict[_Key, _Entry] = {}

    def client(self, key: _Key) -> httpx.Client:
        with self.lock:
            if self.closed:
                raise RuntimeError("Generation connection pool is closed.")
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(httpx.HTTPTransport(trust_env=False))
                self._entries[key] = entry
            entry.borrowers += 1
        borrowed = _BorrowedTransport(self, entry)
        try:
            return httpx.Client(transport=borrowed, trust_env=False)
        except BaseException:
            borrowed.close()
            raise

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for entry in self._entries.values():
                if entry.borrowers == 0:
                    entry.transport.close()
            self._entries.clear()


_active_pool: ContextVar[_Pool | None] = ContextVar("generation_connection_pool", default=None)


def generation_client(
    binding: ResolvedProjectBinding, connection: GenerationConnection, actor: str
) -> httpx.Client:
    pool = _active_pool.get()
    if pool is None:
        return httpx.Client(trust_env=False)
    return pool.client((binding, connection.binding(), actor))


@contextmanager
def reuse_generation_connections() -> Iterator[None]:
    pool = _Pool()
    token = _active_pool.set(pool)
    try:
        yield
    finally:
        _active_pool.reset(token)
        pool.close()
