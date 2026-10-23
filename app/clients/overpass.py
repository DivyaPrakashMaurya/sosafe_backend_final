"""OpenStreetMap's Overpass API: where the police stations, hospitals and the like near a spot are.

It is a free public service, so the manners matter: the questions are about a ~1 km square (the same answer
serves everyone in it, and is kept for hours), the servers are tried in turn, and after a run of failures it is
left alone for a while (the same breaker as Ola). Nothing about who asked, and no exact position, is ever sent.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import httpx
import time

from ..config import Settings
from ..context import request_id_var
from ..domain.nearby import build_group_query, parse_elements
from .ola import CircuitBreaker

logger = logging.getLogger("app.overpass")


class PlacesUnavailable(Exception):
    """OpenStreetMap could not answer. `reason`: timeout, error, status, bad_response or circuit_open.
    Safe to show: it never carries a URL."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OverpassClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        breaker: Optional[CircuitBreaker] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._http = http
        self._urls = tuple(settings.overpass_urls)
        self._timeout = settings.overpass_timeout
        self._connect_timeout = settings.ola_connect_timeout
        self.breaker = breaker or CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_seconds, clock)

    async def places(self, group: str, cell_lat: float, cell_lng: float, radius_m: int) -> dict[str, list[dict[str, Any]]]:
        """Every kind of place in this group around the centre of a cell, by kind, as the app reads them (no
        distances yet). One question for the whole group."""
        if not self._urls:
            raise PlacesUnavailable("error")
        if not self.breaker.allow():
            raise PlacesUnavailable("circuit_open")
        try:
            return await self._ask(group, build_group_query(group, cell_lat, cell_lng, radius_m))
        finally:
            self.breaker.release()

    async def _ask(self, group: str, query: str) -> dict[str, list[dict[str, Any]]]:
        limits = httpx.Timeout(self._timeout, connect=min(self._connect_timeout, self._timeout))
        headers = {"X-Request-Id": request_id_var.get() or ""}
        failure = "error"
        for url in self._urls:
            try:
                response = await self._http.post(url, data={"data": query}, headers=headers, timeout=limits)
            except httpx.TimeoutException:
                failure = "timeout"
                continue
            except httpx.TransportError:
                failure = "error"
                continue

            if response.status_code != 200:
                # 429 (too busy), 504 (its own timeout), 5xx: another server may do better. A 400 means our
                # question is wrong, which another server will not fix, but trying costs nothing.
                failure = "status"
                logger.warning("Overpass call failed", extra={"upstream": "overpass", "status": response.status_code})
                continue

            data = _json_object(response)
            if data is None or not isinstance(data.get("elements"), list):
                failure = "bad_response"
                continue
            remark = data.get("remark")
            if isinstance(remark, str) and "runtime error" in remark.lower():
                # Overpass says 200 even when it ran out of time or memory, with the answer cut short.
                failure = "status"
                logger.warning("Overpass gave a partial answer", extra={"upstream": "overpass"})
                continue

            self.breaker.success()
            return parse_elements(data["elements"], group)

        self.breaker.failure()
        raise PlacesUnavailable(failure)


def _json_object(response: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
