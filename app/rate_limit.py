"""How many requests one person (or, when not signed in, one network address)
may make per minute.

Counted in Redis when it is there, so every instance behind the load balancer
shares one count; in memory otherwise (then each process counts on its own,
which is looser, never stricter). A Redis problem never blocks a request.

Counters are kept under a hash of the user id / address, never the id itself,
and expire a minute later.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .cache import NAMESPACE, RedisStore

logger = logging.getLogger("app.rate_limit")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds until the window resets


def bucket_id(scope: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{scope}:{digest}"


class RateLimiter:
    def __init__(
        self,
        redis_store: Optional[RedisStore] = None,
        enabled: bool = True,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.time,
    ):
        self._redis = redis_store
        self.enabled = enabled
        self._window = window_seconds
        self._clock = clock
        self._memory: dict[str, int] = {}
        self._memory_window = -1

    async def hit(self, bucket: str, limit: int) -> Decision:
        """Counts one request in `bucket` and says whether it is within `limit`."""
        if not self.enabled:
            return Decision(True, limit, limit, 0)
        now = self._clock()
        window = int(now // self._window)
        retry_after = max(1, int(self._window - (now % self._window)))
        count = await self._count(f"{NAMESPACE}rl:{bucket}:{window}", window)
        return Decision(count <= limit, limit, max(0, limit - count), retry_after)

    async def _count(self, key: str, window: int) -> int:
        store = self._redis
        if store is not None and store.available:
            try:
                pipe = store.client.pipeline(transaction=True)
                pipe.incr(key)
                pipe.expire(key, self._window + 5)
                count, _ = await pipe.execute()
                return int(count)
            except Exception as error:  # noqa: BLE001 - fall back, never block
                store.failed("rate-limit count", error)
        if window != self._memory_window:  # a new minute: the old counts are done with
            self._memory.clear()
            self._memory_window = window
        self._memory[key] = self._memory.get(key, 0) + 1
        return self._memory[key]
