"""The four lookups, each through the cache and then Ola. What is never kept:
an error, an empty search, a place Ola cannot locate, a spot with no address,
a route with no line."""

from __future__ import annotations

from typing import Any, Optional

from ..cache import LookupCache, coordinates_key, normalize_query, reverse_key, route_key, search_key
from ..clients.ola import OlaClient
from ..domain.route_schedule import route_response


class PlacesService:
    def __init__(self, ola: OlaClient, cache: LookupCache):
        self._ola = ola
        self._cache = cache

    async def search(self, query: str) -> tuple[list[dict[str, str]], str]:
        if not normalize_query(query):
            return [], "off"  # nothing to look for
        return await self._cache.get_or_fetch(
            "search",
            search_key(query),
            lambda: self._ola.autocomplete(query.strip()),
            keep=lambda results: bool(results),
        )

    async def location(self, place_id: str) -> tuple[Optional[dict[str, float]], str]:
        place_id = place_id.strip()
        return await self._cache.get_or_fetch(
            "coordinates",
            coordinates_key(place_id),
            lambda: self._ola.place_location(place_id),
            keep=lambda found: found is not None,
        )

    async def address(self, lat: float, lng: float) -> tuple[Optional[str], str]:
        return await self._cache.get_or_fetch(
            "reverse",
            reverse_key(lat, lng),
            lambda: self._ola.reverse_geocode(lat, lng),
            keep=lambda address: bool(address),
        )

    async def route(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        mode: str,
        ola_mode: str,
        added_buffer_mins: int,
        checkpoint_count: int,
    ) -> tuple[Optional[dict[str, Any]], str]:
        # Only Ola's raw answer is cached (briefly); the schedule is this request's.
        raw, how = await self._cache.get_or_fetch(
            "route",
            route_key(ola_mode, start, end),
            lambda: self._ola.directions(start, end, ola_mode),
            keep=lambda found: found is not None,
        )
        if raw is None:
            return None, how
        return route_response(raw, mode, added_buffer_mins, checkpoint_count), how
