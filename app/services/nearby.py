"""Nearby safe places, through the cache and then OpenStreetMap.

One answer is kept per GROUP of places, radius and ~1 km square, for hours (police stations do not move), so a
city's worth of people asking from the same streets cost one question. The person's own position is only used
here, to measure the distances. What is never kept: an error, an empty answer (it may be a hiccup, and asking
again is free). Nothing here is paid: the places come from OpenStreetMap.
"""

from __future__ import annotations

from typing import Any

from ..cache import LookupCache, nearby_key
from ..clients.overpass import OverpassClient
from ..domain.nearby import cell_of, group_of, nearest


class NearbyService:
    def __init__(self, overpass: OverpassClient, cache: LookupCache):
        self._overpass = overpass
        self._cache = cache

    async def find(self, category: str, lat: float, lng: float, radius_m: int) -> tuple[list[dict[str, Any]], str]:
        """The nearest places of this kind within `radius_m` of (lat, lng), nearest first, and how the answer
        was got ('hit', 'miss', 'shared' or 'off', as for the other lookups).

        The answer kept is for the whole group the kind belongs to (all the emergency services, or all the
        roadside ones), so the next tap on a sibling kind is a 'hit'."""
        group = group_of(category)
        cell_lat, cell_lng = cell_of(lat, lng)
        kept, how = await self._cache.get_or_fetch(
            "nearby",
            nearby_key(group, radius_m, cell_lat, cell_lng),
            lambda: self._overpass.places(group, cell_lat, cell_lng, radius_m),
            # Nothing at all in a whole group may be a hiccup, and asking again is free: not kept.
            keep=lambda by_kind: any(by_kind.values()),
        )
        return nearest(kept.get(category, []), lat, lng, radius_m), how
