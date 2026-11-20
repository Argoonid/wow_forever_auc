import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


class SnapshotMarketCache:
    """In-memory кэш облачного рынка с автосбросом при новом срезе и LRU-лимитом для графиков."""

    def __init__(self, version_check_interval: float = 60.0, max_history_entries: int = 40) -> None:
        self._check_interval = version_check_interval
        self._max_history = max_history_entries

        self._last_check_ts: float = 0.0
        self._known_snapshot_id: int | None = None
        self._version_lock = asyncio.Lock()

        self._core: dict[str, Any] = {}
        self._history: OrderedDict[str, Any] = OrderedDict()
        self._key_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    async def ensure_fresh(self, get_latest_snapshot_id: Callable[[], Awaitable[int | None]]) -> None:
        now = time.monotonic()
        if self._known_snapshot_id is not None and (now - self._last_check_ts) < self._check_interval:
            return

        async with self._version_lock:
            now = time.monotonic()
            if self._known_snapshot_id is not None and (now - self._last_check_ts) < self._check_interval:
                return

            latest_id = await get_latest_snapshot_id()
            self._last_check_ts = time.monotonic()

            if latest_id != self._known_snapshot_id:
                self._core.clear()
                self._history.clear()
                self._known_snapshot_id = latest_id

    async def get_core(
        self,
        key: str,
        get_latest_snapshot_id: Callable[[], Awaitable[int | None]],
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        await self.ensure_fresh(get_latest_snapshot_id)
        if key in self._core:
            return self._core[key]

        async with self._get_lock(f"core:{key}"):
            if key in self._core:
                return self._core[key]
            value = await fetcher()
            self._core[key] = value
            return value

    async def get_history(
        self,
        key: str,
        get_latest_snapshot_id: Callable[[], Awaitable[int | None]],
        fetcher: Callable[[], Awaitable[Any]],
    ) -> Any:
        await self.ensure_fresh(get_latest_snapshot_id)
        if key in self._history:
            self._history.move_to_end(key)
            return self._history[key]

        async with self._get_lock(f"hist:{key}"):
            if key in self._history:
                self._history.move_to_end(key)
                return self._history[key]
            value = await fetcher()
            self._history[key] = value
            self._history.move_to_end(key)
            while len(self._history) > self._max_history:
                self._history.popitem(last=False)
            return value


market_cache = SnapshotMarketCache(version_check_interval=60.0, max_history_entries=40)