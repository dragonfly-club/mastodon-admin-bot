from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class HeldKeyLock:
    def __init__(self, manager: KeyedAsyncLocks, key: str, entry: _LockEntry) -> None:
        self._manager = manager
        self._key = key
        self._entry = entry
        self._released = False

    async def __aenter__(self) -> HeldKeyLock:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager.release(self._key, self._entry)


class KeyedAsyncLocks:
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[str, _LockEntry] = {}

    async def acquire(self, key: str) -> HeldKeyLock:
        entry = await self._reserve(key)
        try:
            await entry.lock.acquire()
        except BaseException:
            await self._cancel_reservation(key, entry)
            raise
        return HeldKeyLock(self, key, entry)

    async def try_acquire(self, key: str) -> HeldKeyLock | None:
        async with self._guard:
            entry = self._locks.get(key)
            if entry is not None and entry.lock.locked():
                return None
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._locks[key] = entry
            entry.users += 1
        await entry.lock.acquire()
        return HeldKeyLock(self, key, entry)

    async def _reserve(self, key: str) -> _LockEntry:
        async with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._locks[key] = entry
            entry.users += 1
            return entry

    async def release(self, key: str, entry: _LockEntry) -> None:
        if entry.lock.locked():
            entry.lock.release()
        async with self._guard:
            entry.users -= 1
            if entry.users == 0 and not entry.lock.locked():
                current = self._locks.get(key)
                if current is entry:
                    del self._locks[key]

    async def _cancel_reservation(self, key: str, entry: _LockEntry) -> None:
        async with self._guard:
            entry.users -= 1
            if entry.users == 0 and not entry.lock.locked() and self._locks.get(key) is entry:
                del self._locks[key]
