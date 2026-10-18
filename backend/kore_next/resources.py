from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class _ReadWriteLock:
    """Writer-preferring read/write lock for one canonical filesystem path."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            while self._writer or self._waiting_writers:
                await self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    await self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


class ResourceAuthority:
    """Coordinates access to shared resources across concurrent runs."""

    def __init__(
        self,
        *,
        network_concurrency: int = 4,
        mcp_server_concurrency: int = 2,
    ) -> None:
        self.network_concurrency = max(1, network_concurrency)
        self.mcp_server_concurrency = max(1, mcp_server_concurrency)
        self._paths: dict[str, _ReadWriteLock] = {}
        self._serial: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._workspace = _ReadWriteLock()

    @staticmethod
    def canonical_path(path: Path) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    def _path_lock(self, path: Path) -> _ReadWriteLock:
        key = self.canonical_path(path)
        return self._paths.setdefault(key, _ReadWriteLock())

    def _serial_lock(self, name: str) -> asyncio.Lock:
        return self._serial.setdefault(name, asyncio.Lock())

    def _semaphore(self, name: str, capacity: int) -> asyncio.Semaphore:
        return self._semaphores.setdefault(name, asyncio.Semaphore(capacity))

    @asynccontextmanager
    async def guard(
        self,
        tool_name: str,
        *,
        path: Path | None = None,
    ) -> AsyncIterator[None]:
        if tool_name == "shell":
            async with self._workspace.write():
                yield
            return
        if tool_name.startswith("computer_"):
            async with self._serial_lock("desktop"):
                yield
            return
        if tool_name.startswith("mcp__"):
            server = tool_name.split("__", 2)[1] if "__" in tool_name else "default"
            async with self._semaphore(f"mcp:{server}", self.mcp_server_concurrency):
                yield
            return
        if tool_name in {"web_search", "http_request"} or (tool_name == "artifact_publish" and path is None):
            async with self._semaphore("network", self.network_concurrency):
                yield
            return
        if path is not None:
            lock = self._path_lock(path)
            async with self._workspace.read():
                if tool_name == "write_file":
                    async with lock.write():
                        yield
                else:
                    async with lock.read():
                        yield
            return
        yield
