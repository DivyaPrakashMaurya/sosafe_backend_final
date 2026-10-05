"""Answers from Ola Maps, kept for a while so the same question is not paid for twice.

A traveller who goes to the same place again and again (home, college, office)
asks the same few questions each time, and so do thousands of others heading
for the same station.

Where answers are kept:
  * Redis when REDIS_URL is set: shared by every process and every instance,
    survives a restart. This is what lets the backend run as many copies as
    the load needs behind the load balancer.
  * Otherwise (or while Redis is down) in this process's memory.

Rules, because this is a safety app:
  * A cache problem never becomes an API problem: Redis down, slow or a damaged
    entry is a MISS, and the question goes to Ola as if there were no cache.
  * Only good answers are kept (the caller decides: never an error, an empty
    result, a "not found").
  * Many requests for the same missing answer at the same moment cause ONE call
    to Ola (per process); the others wait for it.
  * Nothing personal is logged: no searched text, no coordinates, no Redis URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Mapping, Optional

logger = logging.getLogger("app.cache")

NAMESPACE = "sosafe:v1:"


# ─── Keys: same question -> same key; different question -> different key ───

def _coord(value: float) -> str:
    # 5 decimals is about a metre. "+ 0.0" turns -0.0 into 0.0 so both spell the same.
    return f"{round(float(value), 5) + 0.0:.5f}"


def normalize_query(query: str) -> str:
    """'  Hazratganj   Market ' and 'hazratganj market' are the same search."""
    return " ".join(str(query).lower().split())[:200]


def search_key(query: str) -> Optional[str]:
    text = normalize_query(query)
    return f"{NAMESPACE}search:{text}" if text else None


def coordinates_key(place_id: str) -> Optional[str]:
    pid = str(place_id).strip()
    if not pid:
        return None
    if len(pid) > 120:  # keeps keys a sane size; a SHA-256 does not collide in practice
        pid = "sha256-" + hashlib.sha256(pid.encode("utf-8")).hexdigest()
    return f"{NAMESPACE}coordinates:{pid}"


def reverse_key(lat: float, lng: float) -> str:
    return f"{NAMESPACE}reverse:{_coord(lat)},{_coord(lng)}"


def route_key(mode: str, start: tuple[float, float], end: tuple[float, float]) -> str:
    return f"{NAMESPACE}route:{mode}:{_coord(start[0])},{_coord(start[1])}>{_coord(end[0])},{_coord(end[1])}"


# ─── Stores ───

class MemoryStore:
    """In-process: entries expire, and the least recently used go first when full."""

    def __init__(self, max_entries: int = 5000, clock: Callable[[], float] = time.monotonic):
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._items: "OrderedDict[str, tuple[float, str]]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str) -> Optional[str]:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._items[key] = (self._clock() + ttl_seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


class RedisStore:
    """Redis (asyncio client) behind a breaker: the first failure pauses it for
    `cooldown` seconds, so a Redis that is down costs one short timeout, not one
    per request. While paused, the cache uses memory."""

    def __init__(self, client: Any, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self._cooldown = cooldown
        self._clock = clock
        self._down_until = 0.0

    @property
    def available(self) -> bool:
        return self._clock() >= self._down_until

    def failed(self, what: str, error: BaseException) -> None:
        self._down_until = self._clock() + self._cooldown
        # The error's type only: its text can carry the server's address.
        logger.warning(
            "Redis %s failed (%s); using memory for %ss", what, type(error).__name__, int(self._cooldown),
            extra={"event": "redis_failed"},
        )

    async def get(self, key: str) -> Optional[str]:
        if not self.available:
            return None
        try:
            value = await self.client.get(key)
        except Exception as error:  # noqa: BLE001 - anything at all is "no cache"
            self.failed("read", error)
            return None
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", errors="replace")
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> bool:
        if not self.available:
            return False
        try:
            await self.client.set(key, value, ex=int(ttl_seconds))
            return True
        except Exception as error:  # noqa: BLE001
            self.failed("write", error)
            return False


# ─── The cache the services use ───

class LookupCache:
    """get / put / get_or_fetch of JSON-able answers. Never raises (a fetch's own
    exception, from get_or_fetch, is the only thing that comes out)."""

    def __init__(
        self,
        redis_store: Optional[RedisStore] = None,
        memory_store: Optional[MemoryStore] = None,
        ttl_seconds: Optional[Mapping[str, int]] = None,
        enabled: bool = True,
    ):
        self._redis = redis_store
        self._memory = memory_store if memory_store is not None else MemoryStore()
        self._ttl = dict(ttl_seconds or {})
        self.enabled = enabled
        self._inflight: dict[str, asyncio.Task] = {}

    @property
    def redis(self) -> Optional[RedisStore]:
        return self._redis

    def ttl_for(self, kind: str) -> int:
        return int(self._ttl.get(kind, 0))

    def _usable(self, kind: str, key: Optional[str]) -> bool:
        return self.enabled and key is not None and self.ttl_for(kind) > 0

    async def get(self, kind: str, key: Optional[str]) -> Optional[Any]:
        """The kept answer, or None (a miss, whatever the reason)."""
        if not self._usable(kind, key):
            return None
        try:
            if self._redis is not None and self._redis.available:
                raw = await self._redis.get(key)
            else:
                raw = self._memory.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as error:  # noqa: BLE001 - a damaged entry is a miss
            logger.warning("cache read failed (%s)", type(error).__name__, extra={"kind": kind})
            return None

    async def put(self, kind: str, key: Optional[str], value: Any) -> None:
        """Keep an answer the caller has decided is a good one."""
        if not self._usable(kind, key):
            return
        ttl = self.ttl_for(kind)
        try:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if self._redis is not None and self._redis.available:
                if await self._redis.set(key, raw, ttl):
                    return
            self._memory.set(key, raw, ttl)  # no Redis, or it refused: keep it here
        except Exception as error:  # noqa: BLE001
            logger.warning("cache write failed (%s)", type(error).__name__, extra={"kind": kind})

    async def get_or_fetch(
        self,
        kind: str,
        key: Optional[str],
        fetch: Callable[[], Awaitable[Any]],
        keep: Callable[[Any], bool],
    ) -> tuple[Any, str]:
        """The answer, and how it was got: 'hit' (kept), 'miss' (asked Ola),
        'shared' (another request was already asking Ola; this one waited for it)
        or 'off' (not cached at all).

        The call to Ola runs as its own task, so a request that gives up (the
        phone went away) does not cancel the answer the others are waiting for.
        """
        if not self._usable(kind, key):
            return await fetch(), "off"

        cached = await self.get(kind, key)
        if cached is not None:
            return cached, "hit"

        task = self._inflight.get(key)
        how = "shared"
        if task is None:
            how = "miss"

            async def fetch_and_keep() -> Any:
                value = await fetch()
                if keep(value):
                    await self.put(kind, key, value)
                return value

            task = asyncio.ensure_future(fetch_and_keep())
            self._inflight[key] = task

            def done(finished: asyncio.Task, key: str = key) -> None:
                if self._inflight.get(key) is finished:
                    del self._inflight[key]
                if not finished.cancelled():
                    finished.exception()  # retrieved: no "never retrieved" warning when nobody waited

            task.add_done_callback(done)

        return await asyncio.shield(task), how


def make_cache(settings: Any, redis_client: Any = None) -> LookupCache:
    memory = MemoryStore(max_entries=settings.cache_memory_max_entries)
    store = RedisStore(redis_client) if redis_client is not None else None
    return LookupCache(
        redis_store=store, memory_store=memory, ttl_seconds=settings.cache_ttl_seconds, enabled=settings.cache_enabled
    )
