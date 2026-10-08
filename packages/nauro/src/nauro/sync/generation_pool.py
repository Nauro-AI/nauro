"""Lifetime-scoped connection pools with operation-owned HTTP clients."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock

import httpx

from nauro.store.resolution import ResolvedProjectBinding
from nauro.sync.generation_credentials import GenerationConnection

_Key = tuple[ResolvedProjectBinding, str, str]
_MAX_IDLE_TRANSPORTS = 8


@dataclass
class _Entry:
    transport: httpx.BaseTransport
    borrowers: int = 0


class _BorrowedTransport(httpx.BaseTransport):
    def __init__(self, pool: _Pool, key: _Key, entry: _Entry) -> None:
        self._pool = pool
        self._key = key
        self._entry = entry
        self._closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._entry.transport.handle_request(request)

    def close(self) -> None:
        with self._pool.lock:
            if not self._closed:
                self._closed = True
                self._entry.borrowers -= 1
                if self._entry.borrowers == 0:
                    self._pool.release(self._key, self._entry)


class _Pool:
    def __init__(self) -> None:
        self.lock = Lock()
        self.closed = False
        self._entries: dict[_Key, _Entry] = {}
        self._idle: OrderedDict[_Key, _Entry] = OrderedDict()

    def client(self, key: _Key) -> httpx.Client:
        with self.lock:
            if self.closed:
                raise RuntimeError("Generation connection pool is closed.")
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(httpx.HTTPTransport(trust_env=False))
                self._entries[key] = entry
            self._idle.pop(key, None)
            entry.borrowers += 1
        borrowed = _BorrowedTransport(self, key, entry)
        try:
            return httpx.Client(transport=borrowed, trust_env=False)
        except BaseException:
            borrowed.close()
            raise

    def release(self, key: _Key, entry: _Entry) -> None:
        if self.closed:
            entry.transport.close()
            return
        self._idle[key] = entry
        if len(self._idle) > _MAX_IDLE_TRANSPORTS:
            oldest, retired = self._idle.popitem(last=False)
            del self._entries[oldest]
            retired.transport.close()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for entry in self._entries.values():
                if entry.borrowers == 0:
                    entry.transport.close()
            self._entries.clear()
            self._idle.clear()


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
