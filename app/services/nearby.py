"""Nearby safe places, through the cache and then OpenStreetMap.

One answer is kept per GROUP of places, radius and ~1 km square (police stations do not move), so a city's worth of
people asking from the same streets cost one question. The person's own position is only used here, to measure the
distances. Nothing here is paid: the places come from OpenStreetMap.

How an answer ages (the public map servers are often overloaded, so an old answer is worth keeping):
  * younger than `fresh_seconds` (12 hours): used as it is ("hit");
  * older: asked for again, and if the map servers cannot be reached, the old one is served anyway ("stale"),
    for as long as the cache keeps it (7 days);
  * never kept: an error, or an answer with nothing in it (it may be a hiccup, and asking again is free).

Many requests for the same missing answer at the same moment make ONE question to OpenStreetMap.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from ..cache import LookupCache, nearby_key
from ..clients.overpass import OverpassClient, PlacesUnavailable
from ..domain.nearby import cell_of, group_of, nearest

DEFAULT_FRESH_SECONDS = 12 * 3600


class NearbyService:
    def __init__(
        self,
        overpass: OverpassClient,
        cache: LookupCache,
        fresh_seconds: int = DEFAULT_FRESH_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self._overpass = overpass
        self._cache = cache
        self._fresh = fresh_seconds
        self._clock = clock
        self._inflight: dict[str, asyncio.Task] = {}

    async def find(self, category: str, lat: float, lng: float, radius_m: int) -> tuple[list[dict[str, Any]], str]:
        """The nearest places of this kind within `radius_m` of (lat, lng), nearest first, and how the answer was
        got: 'hit' (kept, still fresh), 'miss' (asked OpenStreetMap), 'shared' (another request was already
        asking), 'stale' (OpenStreetMap could not be reached; an older kept answer was used) or 'off' (not kept).

        The answer kept is for the whole group the kind belongs to (all the emergency services, or all the
        roadside ones), so the next tap on a sibling kind is a 'hit'."""
        group = group_of(category)
        cell_lat, cell_lng = cell_of(lat, lng)
        key = nearby_key(group, radius_m, cell_lat, cell_lng)

        stored = _readable(await self._cache.get("nearby", key))
        if stored is not None and self._clock() - stored["at"] < self._fresh:
            return nearest(stored["kinds"].get(category, []), lat, lng, radius_m), "hit"

        try:
            by_kind, how = await self._fetch(key, group, cell_lat, cell_lng, radius_m)
        except PlacesUnavailable:
            if stored is not None:
                return nearest(stored["kinds"].get(category, []), lat, lng, radius_m), "stale"
            raise
        return nearest(by_kind.get(category, []), lat, lng, radius_m), how

    async def _fetch(self, key: str, group: str, cell_lat: float, cell_lng: float, radius_m: int) -> tuple[dict, str]:
        """One question to OpenStreetMap for everyone who wants this answer at the same time. The question runs
        as a task of its own, so a person who gives up (the phone went away) does not cancel it for the others."""
        task = self._inflight.get(key)
        how = "shared"
        if task is None:
            how = "miss" if self._cache.enabled else "off"

            async def ask_and_keep() -> dict:
                by_kind = await self._overpass.places(group, cell_lat, cell_lng, radius_m)
                if any(by_kind.values()):
                    await self._cache.put("nearby", key, {"at": self._clock(), "kinds": by_kind})
                return by_kind

            task = asyncio.ensure_future(ask_and_keep())
            self._inflight[key] = task

            def done(finished: asyncio.Task, key: str = key) -> None:
                if self._inflight.get(key) is finished:
                    del self._inflight[key]
                if not finished.cancelled():
                    finished.exception()  # retrieved: no "never retrieved" warning when nobody waited

            task.add_done_callback(done)
        return await asyncio.shield(task), how


def _readable(value: Any) -> Optional[dict[str, Any]]:
    """A kept answer, if it is one we wrote (a damaged or older-shaped entry is a miss)."""
    if isinstance(value, dict) and isinstance(value.get("at"), (int, float)) and isinstance(value.get("kinds"), dict):
        return value
    return None
